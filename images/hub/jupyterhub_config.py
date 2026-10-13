# The entire hub configuration lives in booth_notebooks.hubconfig, as a testable function of the
# environment the Helm chart sets. The only other thing here is the startup wait for the hub database
# (ARCHITECTURE.md item 66 (c)): JupyterHub exits on its first refused connection, so wait first.
from booth_notebooks.dbwait import wait_for_database
from booth_notebooks.hubconfig import configure

configure(c)  # noqa: F821 - `c` is injected by JupyterHub
wait_for_database(c.JupyterHub.db_url)  # noqa: F821
