# Shared fixtures. As in datasette-google-credentials, the mock Google fixtures live
# in fixtures_google.py (vendored, D22) and ours in fixtures_sheets.py, so
# later tickets can add theirs here without conflicts. The default suite must
# never contact real Google: _block_network (autouse, session) enforces it.
from fixtures_export import export_datasette, export_link  # noqa: F401
from fixtures_google import (  # noqa: F401
    _block_network,
    mock_google,
    service_account_keys,
)
from fixtures_import import data_db, events, import_cred, import_link  # noqa: F401
from fixtures_sheets import (  # noqa: F401
    datasette,
    oauth_credential,
    sa_credential,
)
