// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Source-tracking YAML node tree and loader.

use crate::Error;
use indexmap::IndexMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Provenance for a [`Node::Mapping`]: which file the mapping was
/// originally loaded from and where in that file it begins.
///
/// Used by include resolution to compute directories for relative
/// `+include:` paths, and by diagnostics to point users at the right
/// source location.
#[derive(Clone, Debug)]
pub struct Source {
    /// Path to the source file. `Arc<PathBuf>` so propagating the
    /// path through nested mappings is a cheap refcount bump.
    pub file: Arc<PathBuf>,
    /// Line where this node begins, as reported by `saphyr`
    /// (1-based; 0 means unknown / synthetic).
    pub line: usize,
    /// Column where this node begins (1-based; 0 means unknown).
    pub col: usize,
}

impl Source {
    /// Directory containing [`file`](Self::file). Falls back to `.`
    /// when the path has no parent (e.g. a bare filename).
    pub fn base_dir(&self) -> &Path {
        self.file.parent().unwrap_or_else(|| Path::new("."))
    }
}

/// A YAML node carrying per-mapping source provenance.
///
/// Mirrors [`serde_json::Value`] but attaches a [`Source`] (file +
/// line + col) to every mapping. Sequences and scalars are not
/// annotated; they inherit their enclosing mapping's source by
/// lookup.
#[derive(Clone, Debug)]
pub enum Node {
    Null,
    Bool(bool),
    Number(serde_json::Number),
    String(String),
    Sequence(Vec<Node>),
    Mapping {
        entries: IndexMap<String, Node>,
        source: Source,
    },
}

impl Node {
    /// Convert to [`serde_json::Value`], discarding source info.
    ///
    /// Storage and existing crate APIs operate on `Value`; this is
    /// the boundary conversion at write time.
    pub fn to_json_value(&self) -> serde_json::Value {
        use serde_json::Value;
        match self {
            Node::Null => Value::Null,
            Node::Bool(b) => Value::Bool(*b),
            Node::Number(n) => Value::Number(n.clone()),
            Node::String(s) => Value::String(s.clone()),
            Node::Sequence(items) => Value::Array(items.iter().map(Self::to_json_value).collect()),
            Node::Mapping { entries, .. } => {
                let mut obj = serde_json::Map::new();
                for (k, v) in entries {
                    obj.insert(k.clone(), v.to_json_value());
                }
                Value::Object(obj)
            }
        }
    }
}

/// Load a YAML file into a [`Node`] tree, attaching [`Source`] to
/// every mapping.
///
/// Multi-document YAML files take the first document; an empty file
/// loads as [`Node::Null`].
pub fn load_file(path: &Path) -> Result<Node, Error> {
    let text = std::fs::read_to_string(path)?;
    let file = Arc::new(path.to_path_buf());
    load_str(&text, &file, path)
}

fn load_str(text: &str, file: &Arc<PathBuf>, path: &Path) -> Result<Node, Error> {
    use saphyr::LoadableYamlNode;
    let mut docs = saphyr::MarkedYaml::load_from_str(text).map_err(|e| Error::Yaml {
        path: path.display().to_string(),
        message: e.to_string(),
    })?;
    if docs.is_empty() {
        return Ok(Node::Null);
    }
    from_marked(docs.remove(0), file, path)
}

fn from_marked(y: saphyr::MarkedYaml<'_>, file: &Arc<PathBuf>, path: &Path) -> Result<Node, Error> {
    let start = y.span.start;
    let line = start.line();
    let col = start.col();
    match y.data {
        saphyr::YamlData::Value(scalar) => scalar_to_node(scalar, path),
        saphyr::YamlData::Representation(cow, _style, _tag) => {
            // `early_parse` defaults on, so this branch should be rare
            // in practice; treat the raw text as a string scalar.
            Ok(Node::String(cow.into_owned()))
        }
        saphyr::YamlData::Sequence(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(from_marked(item, file, path)?);
            }
            Ok(Node::Sequence(out))
        }
        saphyr::YamlData::Mapping(map) => {
            let mut entries = IndexMap::with_capacity(map.len());
            for (k, v) in map {
                let key = scalar_key(k, path)?;
                entries.insert(key, from_marked(v, file, path)?);
            }
            Ok(Node::Mapping {
                entries,
                source: Source {
                    file: file.clone(),
                    line,
                    col,
                },
            })
        }
        saphyr::YamlData::Tagged(_tag, inner) => from_marked(*inner, file, path),
        saphyr::YamlData::Alias(_) => Err(Error::Yaml {
            path: path.display().to_string(),
            message: "unresolved YAML alias (anchors not supported)".into(),
        }),
        saphyr::YamlData::BadValue => Err(Error::Yaml {
            path: path.display().to_string(),
            message: "invalid YAML value".into(),
        }),
    }
}

fn scalar_to_node(scalar: saphyr::Scalar<'_>, path: &Path) -> Result<Node, Error> {
    match scalar {
        saphyr::Scalar::Null => Ok(Node::Null),
        saphyr::Scalar::Boolean(b) => Ok(Node::Bool(b)),
        saphyr::Scalar::Integer(i) => Ok(Node::Number(i.into())),
        saphyr::Scalar::FloatingPoint(of) => serde_json::Number::from_f64(of.0)
            .map(Node::Number)
            .ok_or_else(|| Error::Yaml {
                path: path.display().to_string(),
                message: format!("non-finite float: {}", of.0),
            }),
        saphyr::Scalar::String(s) => Ok(Node::String(s.into_owned())),
    }
}

fn scalar_key(y: saphyr::MarkedYaml<'_>, path: &Path) -> Result<String, Error> {
    let make_err = |msg: String| Error::Yaml {
        path: path.display().to_string(),
        message: msg,
    };
    match y.data {
        saphyr::YamlData::Value(saphyr::Scalar::String(s)) => Ok(s.into_owned()),
        saphyr::YamlData::Value(saphyr::Scalar::Integer(i)) => Ok(i.to_string()),
        saphyr::YamlData::Value(saphyr::Scalar::Boolean(b)) => Ok(b.to_string()),
        saphyr::YamlData::Value(saphyr::Scalar::Null) => Ok(String::new()),
        saphyr::YamlData::Value(saphyr::Scalar::FloatingPoint(of)) => Ok(of.0.to_string()),
        saphyr::YamlData::Representation(cow, _, _) => Ok(cow.into_owned()),
        other => Err(make_err(format!(
            "non-scalar mapping key not supported: {other:?}"
        ))),
    }
}
