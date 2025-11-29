use std::iter::Sum;
use std::ops::{Add, AddAssign};

use proc_macro2::{Delimiter, Spacing, TokenStream, TokenTree};
use quote::ToTokens;
use serde::{Deserialize, Serialize};
use syn::visit::{self, Visit};
use syn::{Block, ExprUnsafe, File, ImplItemFn, ItemFn, ItemImpl, Stmt, Visibility};

#[derive(Debug, Default, Deserialize, Serialize)]
pub struct Stats {
    /// Total number of files evaluated
    total_files: u64,

    /// Total number of lines in each Rust file
    total_lines: u64,

    /// Total number of tokens (https://doc.rust-lang.org/stable/reference/tokens.html)
    total_tokens: u64,

    /// Total number of statements
    total_statements: u64,

    /// Scoring rules:
    ///
    /// * +1 for each statement *inside of* an unsafe block or unsafe fn
    /// * +1 for each unsafe fn that is `pub` or part of a trait impl
    /// * +1 for all other uses of the `unsafe` keyword (ie., other than unsafe fn or unsafe blocks)
    ///
    /// The following examples have a score of 1:
    ///
    /// * `unsafe fn foo() { unsafe { stmt(); } }`
    /// * `unsafe fn foo() { stmt(); }`
    /// * `fn foo() { unsafe { stmt(); } }`
    /// * `unsafe impl Sync for Bar {}`
    ///
    /// The following examples have a score of 2:
    ///
    /// * `pub unsafe fn foo() { unsafe { stmt(); } }`
    /// * `pub unsafe fn foo() { stmt(); }`
    /// * `impl MyTrait for Bar { unsafe fn trait_method() { stmt(); } }`
    /// * `unsafe impl MyUnsafeTrait for Bar { unsafe fn trait_method() {} }`
    unsafe_score: u64,

    /// Number of statements inside an unsafe block or unsafe fn
    unsafe_statements: u64,

    /// Number of `unsafe fn ...` (including `pub unsafe fn ...`)
    unsafe_fns: u64,

    /// Number of `pub unsafe fn ...`
    unsafe_pub_fns: u64,

    /// Number of `unsafe { ... }` *blocks*
    unsafe_blocks: u64,

    /// Number of `unsafe impl ...`
    unsafe_impls: u64,

    /// All instances of `unsafe` not otherwise accounted for
    unsafe_other: u64,

    /// Sum of number of lines covered by each unsafe block or unsafe fn.
    ///
    /// Beware, this is an unreliable metric because rustfmt handles line
    /// wrapping in a fragile way, and because this will double-count lines
    /// covered by multiple non-overlapping unsafe blocks!
    unsafe_lines_low_fidelity: u64,
}

impl Stats {
    pub fn evaluate_file(file_path: &std::path::Path) -> Self {
        let file_contents = std::fs::read_to_string(file_path).unwrap();
        let file = syn::parse_file(&file_contents).unwrap();

        let VisitorStats {
            total_statements,
            unsafe_statements,
            unsafe_fns,
            unsafe_pub_fns,
            unsafe_blocks,
            unsafe_impls,
            unsafe_lines,
        } = VisitorStats::from(&file);
        let TokenStats {
            total_tokens,
            total_unsafe_tokens,
        } = TokenStats::from(file);

        let unsafe_other = total_unsafe_tokens - unsafe_fns - unsafe_blocks - unsafe_impls;
        let unsafe_score = unsafe_statements + unsafe_impls + unsafe_other;

        Stats {
            total_files: 1,
            total_lines: file_contents.trim().lines().count() as u64,
            total_tokens,
            total_statements,
            unsafe_score,
            unsafe_lines_low_fidelity: unsafe_lines,
            unsafe_statements,
            unsafe_fns,
            unsafe_pub_fns,
            unsafe_blocks,
            unsafe_impls,
            unsafe_other,
        }
    }
}

impl AddAssign for Stats {
    fn add_assign(&mut self, rhs: Self) {
        self.total_files += rhs.total_files;
        self.total_lines += rhs.total_lines;
        self.total_tokens += rhs.total_tokens;
        self.total_statements += rhs.total_statements;
        self.unsafe_score += rhs.unsafe_score;
        self.unsafe_statements += rhs.unsafe_statements;
        self.unsafe_fns += rhs.unsafe_fns;
        self.unsafe_pub_fns += rhs.unsafe_pub_fns;
        self.unsafe_blocks += rhs.unsafe_blocks;
        self.unsafe_impls += rhs.unsafe_impls;
        self.unsafe_other += rhs.unsafe_other;
        self.unsafe_lines_low_fidelity += rhs.unsafe_lines_low_fidelity;
    }
}

impl Add for Stats {
    type Output = Self;

    fn add(mut self, rhs: Self) -> Self::Output {
        self += rhs;
        self
    }
}

impl Sum for Stats {
    fn sum<I: Iterator<Item = Self>>(iter: I) -> Self {
        let mut res = Self::default();
        for x in iter {
            res += x;
        }
        res
    }
}

#[derive(Debug, Default)]
struct VisitorStats {
    total_statements: u64,
    unsafe_statements: u64,
    unsafe_fns: u64,
    unsafe_pub_fns: u64,
    unsafe_blocks: u64,
    unsafe_impls: u64,
    unsafe_lines: u64,
}

impl From<&File> for VisitorStats {
    fn from(file: &File) -> Self {
        #[derive(Debug, Default)]
        struct UnsafeVisitor {
            inside_unsafe: bool,
            inside_trait_impl: bool,
            stats: VisitorStats,
        }

        fn lines_covered_by_block(block: &Block) -> u64 {
            let span = block.brace_token.span.join();
            let start_line = span.start().line as u64;
            let end_line = span.end().line as u64;
            end_line - start_line + 1
        }

        impl<'ast> Visit<'ast> for UnsafeVisitor {
            fn visit_expr_unsafe(&mut self, node: &'ast ExprUnsafe) {
                let was_inside_unsafe = self.inside_unsafe;
                self.inside_unsafe = true;

                self.stats.unsafe_blocks += 1;
                if !was_inside_unsafe {
                    self.stats.unsafe_lines += lines_covered_by_block(&node.block);
                }

                visit::visit_expr_unsafe(self, node);
                self.inside_unsafe = was_inside_unsafe;
            }

            fn visit_item_fn(&mut self, node: &'ast ItemFn) {
                let was_inside_unsafe = self.inside_unsafe;
                self.inside_unsafe = node.sig.unsafety.is_some();

                if self.inside_unsafe {
                    self.stats.unsafe_fns += 1;
                    if matches!(node.vis, Visibility::Public(_)) {
                        self.stats.unsafe_pub_fns += 1;
                    }
                    if !was_inside_unsafe {
                        self.stats.unsafe_lines += lines_covered_by_block(&node.block);
                    }
                } else if was_inside_unsafe {
                    self.stats.unsafe_lines -= lines_covered_by_block(&node.block);
                }

                visit::visit_item_fn(self, node);
                self.inside_unsafe = was_inside_unsafe;
            }

            fn visit_impl_item_fn(&mut self, node: &'ast ImplItemFn) {
                let was_inside_unsafe = self.inside_unsafe;
                self.inside_unsafe = node.sig.unsafety.is_some();

                if self.inside_unsafe {
                    self.stats.unsafe_fns += 1;
                    if self.inside_trait_impl || matches!(node.vis, Visibility::Public(_)) {
                        self.stats.unsafe_pub_fns += 1;
                    }
                    if !was_inside_unsafe {
                        self.stats.unsafe_lines += lines_covered_by_block(&node.block);
                    }
                } else if was_inside_unsafe {
                    self.stats.unsafe_lines -= lines_covered_by_block(&node.block);
                }

                visit::visit_impl_item_fn(self, node);
                self.inside_unsafe = was_inside_unsafe;
            }

            fn visit_stmt(&mut self, node: &'ast Stmt) {
                self.stats.total_statements += 1;
                if self.inside_unsafe {
                    self.stats.unsafe_statements += 1;
                }
                visit::visit_stmt(self, node);
            }

            fn visit_item_impl(&mut self, node: &'ast ItemImpl) {
                let was_inside_trait_impl = self.inside_trait_impl;
                self.inside_trait_impl = node.trait_.is_some();

                if node.unsafety.is_some() {
                    self.stats.unsafe_impls += 1;
                }

                visit::visit_item_impl(self, node);
                self.inside_trait_impl = was_inside_trait_impl;
            }
        }

        let mut visitor = UnsafeVisitor::default();
        visitor.visit_file(file);
        visitor.stats
    }
}

#[derive(Debug, Default)]
struct TokenStats {
    total_tokens: u64,
    total_unsafe_tokens: u64,
}

impl TokenStats {
    fn count(&mut self, stream: TokenStream) {
        let mut streams = vec![stream.into_token_stream()];
        while let Some(s) = streams.pop() {
            for tt in s.into_token_stream().into_iter() {
                match tt {
                    TokenTree::Group(group) => {
                        self.total_tokens += match group.delimiter() {
                            Delimiter::None => 0,
                            _ => 2,
                        };
                        streams.push(group.stream());
                    }
                    TokenTree::Punct(punct) => {
                        self.total_tokens += match punct.spacing() {
                            Spacing::Alone => 1,
                            Spacing::Joint => 0,
                        }
                    }
                    TokenTree::Ident(id) => {
                        self.total_tokens += 1;
                        if id == "unsafe" {
                            self.total_unsafe_tokens += 1;
                        }
                    }
                    TokenTree::Literal(_) => self.total_tokens += 1,
                }
            }
        }
    }
}

impl<T: ToTokens> From<T> for TokenStats {
    fn from(expr: T) -> Self {
        let mut this = TokenStats::default();
        this.count(expr.into_token_stream());
        this
    }
}
