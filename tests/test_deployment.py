"""
Phase-4 (production readiness) checks for the deployment surface:
Dockerfile, requirements.txt, railway.toml, .env.example.

These are static checks — no containers built.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_uses_pinned_python():
    df = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12" in df, "Dockerfile should pin to python:3.12-*"


def test_dockerfile_no_cache_pip_install():
    df = (ROOT / "Dockerfile").read_text()
    assert "--no-cache-dir" in df, "pip install should use --no-cache-dir"


def test_dockerfile_copies_requirements_before_source_for_layer_caching():
    """Best practice: COPY requirements.txt, RUN pip install, then COPY ."""
    df = (ROOT / "Dockerfile").read_text().splitlines()
    req_copy_idx = next(i for i, l in enumerate(df) if "COPY requirements.txt" in l)
    pip_idx = next(i for i, l in enumerate(df) if "pip install" in l)
    src_copy_idx = next(i for i, l in enumerate(df) if l.strip() == "COPY . .")
    assert req_copy_idx < pip_idx < src_copy_idx


def test_dockerfile_starts_uvicorn():
    df = (ROOT / "Dockerfile").read_text()
    assert "uvicorn main:app" in df
    assert "${PORT" in df, "PORT should be templated for Railway"


def test_dockerfile_exposes_port_8000():
    df = (ROOT / "Dockerfile").read_text()
    assert "EXPOSE 8000" in df


def test_requirements_pin_all_versions():
    """Every requirement should pin an explicit ==X.Y.Z version."""
    reqs = (ROOT / "requirements.txt").read_text().splitlines()
    for line in reqs:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"Unpinned dependency: {line}"


def test_required_packages_listed():
    reqs = (ROOT / "requirements.txt").read_text()
    for pkg in ["fastapi", "uvicorn", "psycopg2-binary",
                "requests", "python-dateutil", "pydantic"]:
        assert pkg in reqs, f"Missing dependency: {pkg}"


def test_uvicorn_with_standard_extras():
    reqs = (ROOT / "requirements.txt").read_text()
    assert "uvicorn[standard]" in reqs


def test_railway_config_uses_dockerfile():
    rt = (ROOT / "railway.toml").read_text()
    assert 'builder = "DOCKERFILE"' in rt


def test_railway_healthcheck_points_to_health_endpoint():
    rt = (ROOT / "railway.toml").read_text()
    assert "/health" in rt


def test_railway_restart_on_failure():
    rt = (ROOT / "railway.toml").read_text()
    assert 'restartPolicyType' in rt
    assert 'ON_FAILURE' in rt


def test_env_example_lists_all_required_vars():
    env = (ROOT / ".env.example").read_text()
    for var in ["DATABASE_URL", "SHOPIFY_STORE",
                "SHOPIFY_TOKEN", "SHOPIFY_API_VERSION",
                "PORTAL_PASSWORD"]:
        assert var in env, f"Missing example env var: {var}"


def test_env_example_warns_about_token_rotation():
    env = (ROOT / ".env.example").read_text().lower()
    assert "rotate" in env or "never reuse" in env, (
        "Token-rotation warning should be present in .env.example"
    )


def test_no_real_secrets_in_env_example():
    """Make sure placeholder values aren't accidentally real."""
    env = (ROOT / ".env.example").read_text()
    # The token line should not look like a real Shopify Admin token.
    # Real tokens are shpat_<32+ hex>. The placeholder is shpat_YOUR_NEW_TOKEN_HERE.
    import re
    real_token = re.search(r"shpat_[a-f0-9]{32,}", env)
    assert real_token is None, "Real-looking token in .env.example!"


def test_frontend_index_exists():
    assert (ROOT / "frontend" / "index.html").exists()
