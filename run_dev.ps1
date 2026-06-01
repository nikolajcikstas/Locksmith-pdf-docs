$ErrorActionPreference = "Stop"

$env:PYTHONPATH = "src"
$env:DATABASE_URL = "postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs"
$env:APP_HOST = "0.0.0.0"
$env:APP_PORT = "8000"

python -m locksmith_docs.web.main
