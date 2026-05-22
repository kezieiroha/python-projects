"""
Pipeline stage modules for the acme DB release pipeline.

Each stage (s1 through s7) is a self-contained Python script with its own CLI
entry point (main()) and a run() function that is also importable for testing.
Stages are executed sequentially by CI; each writes JSON artefacts that
are consumed by the next stage.
"""
