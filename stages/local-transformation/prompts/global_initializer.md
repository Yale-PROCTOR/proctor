+++
id = "global_initializer"
version = 1
description = "Adapt a global initializer to fixed transformed struct definitions."
variables = ["dependency_context", "transformation_targets", "repair_context", "repair_base", "use_xj_scanf_guidance", "libc_guidance", "printf_guidance", "proctor_libc_call_guidance", "foreign_static_guidance"]
+++
Rewrite the requested Rust static or constant initializer generated from C.
The source is the behavioral reference. Source and target type definitions in
Dependency Context describe the original and transformed struct fields. The
target definitions are fixed; use them for construction and field access.

Requirements:

1. Preserve the initialized value and all defined source behavior. Change only
   the initializer. Copy its declaration exactly, including its name, type,
   attributes, visibility, and static mutability. Emit exactly the requested
   global item and no other top-level items.
2. The initializer must be valid in a static or constant context. Use only
   operations permitted by Rust constant evaluation. Do not introduce runtime
   initialization, lazy storage, allocation, helper items, or changed global
   types. Use the target signatures for referenced functions. Required unsafe
   blocks are permitted; use them only for operations that require them.
3. An unlabeled initializer is one editable expression, including any nested
   blocks it contains. Do not introduce proctor labels into it.
4. For a labeled block initializer, complete every `todo!()` hole. Preserve
   complete labeled statements in the Target Skeleton exactly. Keep every
   source label in order at its statement-list level. A statement may expand
   into consecutive siblings with the same label. Preserve the existing
   control forms, their roles, and nested labels. Do not add labels to new
   nested statements. Regions without independent labels inside a labeled
   statement are editable as part of that statement.
5. In labeled blocks, preserve existing binding names, patterns, and the target
   skeleton's local types. Initializer-local pointer kinds are unchanged.
   Name new bindings outside opaque regions `proctor_temp_var_n`, with a
   nonnegative integer `n`, and keep them within their same-label statement
   group. Do not define local functions, types, constants, statics, or imports.
6. Do not return a placeholder initializer or replace the source value with an
   arbitrary zero, null, default, or panic solely to make compilation succeed.
{% if repair_base is not none %}7. {% set patch_guidance %}{% include "partials/repair_patch.jinja" %}{% endset %}{{ patch_guidance | trim | indent(3) }}
{% else %}7. Return exactly one Rust code block delimited by triple-backtick fences,
   containing the complete requested global item and no prose.
{% endif %}
{% if dependency_context %}## Dependency Context

{{ dependency_context }}

{% endif %}## Transformation Target

{{ transformation_targets }}

{{ repair_context -}}
