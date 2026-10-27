+++
id = "unstructured_repair"
version = 1
description = "Repair a complete Rust function SCC using fixed target signatures and freely rewritten bodies."
variables = ["dependency_context", "transformation_targets", "repair_context", "use_xj_scanf_guidance", "libc_guidance", "printf_guidance", "proctor_libc_call_guidance", "foreign_static_guidance"]
+++
You are repairing Rust functions generated from C after constrained transformation
could not produce an accepted result. Rewrite every requested function body using
the original source behavior and the required target signature.

The original implementation is the behavioral reference. A previous failed target,
when supplied, is a starting point for repair. The target specifies only the
required function signature. You may freely change control flow, statement order,
local-variable types, patterns, and names inside the body.

Requirements:

1. Exactly preserve the original source behavior wherever it is defined, including
   apparent bugs and preconditions. Do not add validation, fallback behavior, or
   error handling absent from the original source. If a source dereference assumes
   a nonnull pointer, unwrap the corresponding Option directly.
2. Copy each required target signature exactly, including its lifetime declaration,
   parameter names and types, return type, and unsafe qualifier. Call transformed
   dependencies using their target signatures.
3. Emit every requested function exactly once. Emit no additional top-level or
   function-local items, including imports, helper functions, types, constants,
   statics, or modules. Use fully qualified paths for helpers.
4. Complete every function body. Do not return a whole-function todo stub. Do not
   introduce explicit unsafe blocks, proctor labels, or statement/expression
   attributes.
5. Prefer behavior-equivalent safe Rust functions or methods for foreign calls
   where available; otherwise preserve the foreign call.{% if use_xj_scanf_guidance %}

{% set scanf_guidance %}{% include "partials/xj_scanf.jinja" %}{% endset %}{{ scanf_guidance | trim | indent(3, true) }}{% endif %}{% if libc_guidance %}

   {{ libc_guidance | replace('\n', '\n   ') }}{% endif %}{% if printf_guidance %}

   {{ printf_guidance | replace('\n', '\n   ') }}{% endif %}{% if proctor_libc_call_guidance %}

   {{ proctor_libc_call_guidance | replace('\n', '\n   ') }}{% endif %}{% if foreign_static_guidance %}

   {{ foreign_static_guidance | replace('\n', '\n   ') }}{% endif %}
6. {% set cast_guidance %}{% include "partials/bytemuck.jinja" %}{% endset %}{{ cast_guidance | trim | indent(3) }}
7. Return exactly one triple-backtick fenced Rust code block containing all
   requested functions and no prose.

{% if dependency_context %}## Dependency Context

{{ dependency_context }}

{% endif %}## Transformation Targets

{{ transformation_targets }}

{{ repair_context -}}
