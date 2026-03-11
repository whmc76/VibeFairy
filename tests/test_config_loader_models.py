from __future__ import annotations

from pathlib import Path

from vibefairy.config.loader import load_config


def test_loads_new_models_config(tmp_path: Path):
    config_path = tmp_path / "vibefairy.toml"
    config_path.write_text(
        """
[models.main]
provider = "codex"
model = "o4-mini"
timeout_secs = 111

[models.review]
enabled = true
provider = "gemini"
model = "gemini-2.5-pro"
timeout_secs = 222
readonly_command = ["review-cli", "{working_dir}", "{model}"]
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(config_path=config_path, env_path=tmp_path / ".env")

    assert cfg.models.main.provider == "codex"
    assert cfg.models.main.model == "o4-mini"
    assert cfg.models.main.timeout_secs == 111
    assert cfg.models.review.enabled is True
    assert cfg.models.review.provider == "gemini"
    assert cfg.models.review.model == "gemini-2.5-pro"
    assert cfg.models.review.readonly_command == ["review-cli", "{working_dir}", "{model}"]
