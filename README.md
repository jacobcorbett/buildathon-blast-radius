# Blast Radius

Buildathon entry for the RWX CI test-selection challenge. The selector takes a patch against the pinned sqlglot revision and chooses the tests to run across up to eight workers.

[Official track and scoring rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius)

## Local setup

On Yoga, the official scoring rules and sample kit are in the Git-ignored `.buildathon/` directory. Extract the kit into an empty directory there and run `make sample` to measure the all-tests starter on the 30 public examples.

The scorer builds the repository's top-level Dockerfile for linux/amd64 and runs the selector offline. Each missed failing commit costs 300 simulated seconds. Use coverage and dependency maps from the pinned upstream commit. Keep sample answers out of the submitted selector.
