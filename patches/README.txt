Patches against the ORIGINAL src.zip, one per file (new files runner.py / reposition.py are separate).
From the project root, on top of YOUR current files:

    patch -p1 --dry-run < patches/src_harness_scenario.py.patch     # check first
    patch -p1           < patches/src_harness_scenario.py.patch     # apply

A hunk that fails is written to <file>.rej; the rest is applied.
