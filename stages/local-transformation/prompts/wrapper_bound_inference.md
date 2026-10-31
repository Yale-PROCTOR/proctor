+++
id = "wrapper_bound_inference"
version = 1
description = "Infer placeholder slice lengths in one generated API wrapper."
variables = ["wrapper", "internal", "bounds"]
+++
Infer valid slice-length expressions for the hard-coded bounds in this Rust API
wrapper, using only the wrapper and its internal function below.

Return only a JSON array of strings, one Rust expression per listed bound in
source order. Every expression must have type usize in the wrapper's scope.
Do not change any other expression, null check, signature, function, import, or
API. Do not introduce helper functions or new parameters. Do not guess: when
the correct bound cannot be inferred from this context, return that entry's
original literal unchanged. Do not return Markdown fences or explanations.

Original bounds in source order: {{ bounds | tojson }}

Wrapper:
```rust
{{ wrapper }}
```

Internal function:
```rust
{{ internal }}
```
