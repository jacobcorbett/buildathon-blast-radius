# Blast Radius

A defensive CI test selector for the RWX Buildathon. The root Dockerfile builds an offline Rust executable that reads the scorer's test manifest and patches and emits one JSON line per commit. It selects tests by pytest node ID and uses the scorer's test numbers only for output.

The build profiles the pinned SQLGlot suite with line coverage, shared fixture setup, file reads, and dialect inheritance. It observes work sent to process pools in-process, including the serialization used to pass arguments and results. The build creates the metadata in the image from the upstream revision. The public sample is only used for evaluation.

The selector verifies unified-diff context against the pinned Python source and checks the patched syntax with Tree-sitter. Function-body changes use baseline coverage. Structural changes in dialect components use static eager imports, observed execution and dialect inheritance. Known fixture changes use observed dependencies. Comment edits use source-reader coverage when the parser confirms the executable tokens match. Unknown tests are selected automatically. Unrecognised edits, uncertain syntax and shared structural source changes select the full suite.

The scheduler compares longest-first assignments across one to eight workers in integer milliseconds, including the two-second charge for each worker.

[Official track and scoring rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius)

## Local verification

Extract the official kit into an empty ignored directory such as `.buildathon/starter`. Run the unchanged starter first, then build this repository's root Dockerfile and run the kit's sample target with its build step disabled so it uses the entry image.

```sh
make -C .buildathon/starter sample IMAGE=blast-radius-starter
docker build --platform linux/amd64 --tag blast-radius-entry .
make -C .buildathon/starter -o build sample IMAGE=blast-radius-entry
```

The local public sample has 30 commits. Its score excludes CPU time and is separate from the hidden practice and final scores. Coverage is evidence of baseline execution, so indirect runtime dependencies and changes in reachability can still cause misses.
