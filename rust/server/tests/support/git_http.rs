// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A git server over http for tests: `git http-backend` serving the bare
//! repositories under a directory, with those made private behind Basic
//! auth for one user and token.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::extract::Request;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::Response;
use base64::Engine;

#[derive(Clone)]
pub struct GitHttp {
    /// `http://127.0.0.1:<port>`
    pub base: String,
    private: Arc<Mutex<HashSet<String>>>,
    hidden: Arc<std::sync::atomic::AtomicBool>,
    anonymous: Arc<AtomicUsize>,
}

impl GitHttp {
    /// Serve the bare repositories under `root` (`<root>/org/proj.git`),
    /// the private ones to `user` with `token` alone.
    pub async fn start(root: &Path, user: &str, token: &str) -> Self {
        let server = GitHttp {
            base: String::new(),
            private: Arc::default(),
            hidden: Arc::default(),
            anonymous: Arc::default(),
        };
        let expected = format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD.encode(format!("{user}:{token}"))
        );
        let (root, private, hidden, anonymous) = (
            root.to_path_buf(),
            server.private.clone(),
            server.hidden.clone(),
            server.anonymous.clone(),
        );
        let state = Served {
            root,
            private,
            hidden,
            anonymous,
            expected,
        };
        let app = axum::Router::new()
            .fallback(handle)
            .with_state(Arc::new(state));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        GitHttp {
            base: format!("http://{addr}"),
            ..server
        }
    }

    /// Serve `repo` (`org/proj.git`) only to the user with the token.
    pub fn make_private(&self, repo: &str) {
        self.private.lock().unwrap().insert(repo.to_string());
    }

    /// Answer credentials that aren't the user's with 404, as GitLab
    /// answers a token that can't see a project.
    pub fn hide_from_others(&self) {
        self.hidden.store(true, Ordering::SeqCst);
    }

    /// How many requests came with no credentials.
    pub fn anonymous_requests(&self) -> usize {
        self.anonymous.load(Ordering::SeqCst)
    }
}

/// The repository a request's path is in: `org/proj.git`.
fn repo_of(path: &str) -> String {
    let path = path.trim_start_matches('/');
    match path.find(".git") {
        Some(end) => path[..end + 4].to_string(),
        None => path.to_string(),
    }
}

struct Served {
    root: PathBuf,
    private: Arc<Mutex<HashSet<String>>>,
    hidden: Arc<std::sync::atomic::AtomicBool>,
    anonymous: Arc<AtomicUsize>,
    expected: String,
}

async fn handle(
    axum::extract::State(served): axum::extract::State<Arc<Served>>,
    req: Request,
) -> Response {
    let authorization = req
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    if authorization.is_none() {
        served.anonymous.fetch_add(1, Ordering::SeqCst);
    }
    let path = req.uri().path().to_string();
    let private = served.private.lock().unwrap().contains(&repo_of(&path));
    if private
        && authorization.is_some()
        && authorization.as_deref() != Some(served.expected.as_str())
        && served.hidden.load(Ordering::SeqCst)
    {
        let mut not_found = Response::new(Body::from(
            "The project you were looking for could not be found or you don't have permission to view it.",
        ));
        *not_found.status_mut() = StatusCode::NOT_FOUND;
        return not_found;
    }
    if private && authorization.as_deref() != Some(served.expected.as_str()) {
        let mut refused = Response::new(Body::from("auth required"));
        *refused.status_mut() = StatusCode::UNAUTHORIZED;
        refused.headers_mut().insert(
            header::WWW_AUTHENTICATE,
            HeaderValue::from_static("Basic realm=\"git\""),
        );
        return refused;
    }
    cgi(req, &served.root, &path).await
}

/// `git http-backend`'s response to `req`.
async fn cgi(req: Request, root: &Path, path: &str) -> Response {
    let (content_type, encoding) = {
        let header = |name: header::HeaderName| {
            req.headers()
                .get(name)
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default()
                .to_string()
        };
        (
            header(header::CONTENT_TYPE),
            header(header::CONTENT_ENCODING),
        )
    };
    let method = req.method().to_string();
    let query = req.uri().query().unwrap_or_default().to_string();
    let body = axum::body::to_bytes(req.into_body(), usize::MAX)
        .await
        .unwrap();
    let mut child = tokio::process::Command::new("git")
        .arg("http-backend")
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("GIT_PROJECT_ROOT", root)
        .env("GIT_HTTP_EXPORT_ALL", "1")
        .env("PATH_INFO", path)
        .env("QUERY_STRING", query)
        .env("REQUEST_METHOD", method)
        .env("CONTENT_TYPE", content_type)
        .env("HTTP_CONTENT_ENCODING", encoding)
        .env("CONTENT_LENGTH", body.len().to_string())
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .expect("git http-backend");
    let mut stdin = child.stdin.take().unwrap();
    tokio::io::AsyncWriteExt::write_all(&mut stdin, &body)
        .await
        .unwrap();
    drop(stdin);
    let out = child.wait_with_output().await.unwrap().stdout;
    let split = out
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|i| (i, 4))
        .or_else(|| out.windows(2).position(|w| w == b"\n\n").map(|i| (i, 2)))
        .expect("cgi headers");
    let (head, rest) = (&out[..split.0], &out[split.0 + split.1..]);
    let mut response = Response::new(Body::from(rest.to_vec()));
    for line in String::from_utf8_lossy(head).lines() {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        let value = value.trim();
        if name.eq_ignore_ascii_case("Status") {
            let code = value.split_whitespace().next().unwrap_or("200");
            *response.status_mut() = StatusCode::from_u16(code.parse().unwrap()).unwrap();
        } else if let (Ok(name), Ok(value)) = (
            header::HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_str(value),
        ) {
            response.headers_mut().append(name, value);
        }
    }
    response
}
