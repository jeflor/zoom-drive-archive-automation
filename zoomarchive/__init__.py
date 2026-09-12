"""Pure decision logic for the Zoom → Drive archive pipeline.

Everything in this package is side-effect free: no network, no filesystem, no
credentials. The rules that decide *whether a recording may be deleted* live
here, so they can be unit-tested in isolation — the scripts in `scripts/` do
the I/O and call in here for the verdict.
"""
