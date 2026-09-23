# The entire hub configuration lives in booth_notebooks.hubconfig, as a testable function of the
# environment the Helm chart sets. Nothing belongs here.
from booth_notebooks.hubconfig import configure

configure(c)  # noqa: F821 - `c` is injected by JupyterHub
