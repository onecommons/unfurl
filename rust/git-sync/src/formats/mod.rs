// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Concrete [`crate::DataFormat`] implementations bundled with the crate.
//!
//! Currently a single submodule, [`cloudmap`], implementing
//! [`cloudmap::CloudMapFormat`]. Add more formats here, or out-of-tree
//! by implementing [`crate::DataFormat`] and calling
//! [`crate::FormatRegistry::register`].

pub mod cloudmap;
pub mod cloudmap_types;
