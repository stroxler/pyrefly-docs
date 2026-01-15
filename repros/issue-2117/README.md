# Repro for Pyrefly issue 2117

In a fresh env, `uv add pyrefly pandas` produces some weird behavior;
the IDE and CLI get different types (IDE seems more wrong, but the
divergence is a bigger problem than the specific typing behavior and
they're obviously related)

In the IDE, go-to-type-def is giving me fishy rtesults, it seems to
point at a vendored `pandas-stubs/core/dtypes -> ExtensionDtype`
which does not at all seem to be a Dataframe. Unclear whether that's
related.
