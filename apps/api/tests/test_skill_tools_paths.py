import hashlib

import pytest

from app.agent.model_context.types import coerce_tool_output
from app.mcp_tools import skill_tools
from app.mcp_tools.registry import registry


def _value(result):
    return coerce_tool_output(result).value


def _write_skill(root, directory, *, name, description, body="Follow it.\n", metadata=""):
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{metadata}"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_standard_skill_catalog_and_read_use_codex_handles(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENREEL_SKILLS_DIR", raising=False)
    monkeypatch.setattr(skill_tools.settings, "PROJECT_ROOT", str(tmp_path))
    _write_skill(
        tmp_path / "skills",
        "workflows/custom_flow",
        name="custom_flow",
        description="Use for custom video workflow requests.",
        body="Run the custom workflow.\n",
        metadata="category: workflow\n",
    )

    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    item = next(item for item in listed["skills"] if item["name"] == "custom_flow")
    assert item == {
        "authority": {"kind": "orchestrator"},
        "package": "user/workflows/custom_flow",
        "name": "custom_flow",
        "description": "Use for custom video workflow requests.",
        "main_resource": "SKILL.md",
    }

    loaded = _value(
        await skill_tools.skills_read(
            item["authority"], item["package"], item["main_resource"]
        )
    )
    assert set(loaded) == {"resource", "contents", "next_cursor"}
    assert loaded["resource"] == "SKILL.md"
    assert "name: custom_flow" in loaded["contents"]
    assert "Run the custom workflow" in loaded["contents"]
    assert loaded["next_cursor"] is None


def test_skill_resource_tool_contracts_match_codex() -> None:
    list_spec = registry.get("skills.list")
    read_spec = registry.get("skills.read")
    assert list_spec is not None
    assert read_spec is not None
    assert list_spec.description == (
        "List skills owned by the requested authority. Returns the exact authority, package, and "
        "main_resource values required by skills.read. Pass next_cursor back as cursor to continue."
    )
    assert read_spec.description == (
        "Read one page from a skill resource. Pass the exact authority and package from skills.list "
        "or an explicitly selected skill's resource_access metadata, plus its main_resource or a "
        "referenced resource beneath that package. Pass next_cursor back as cursor to continue."
    )
    list_authorities = list_spec.schema["properties"]["authority"]["oneOf"]
    read_authorities = read_spec.schema["properties"]["authority"]["oneOf"]
    assert [item["properties"]["kind"]["const"] for item in list_authorities] == [
        "orchestrator",
        "executor",
    ]
    assert read_authorities[0]["required"] == ["kind"]
    assert read_authorities[1]["required"] == ["kind", "id"]
    assert list_spec.output_policy.max_model_tokens == 10_000
    assert read_spec.output_policy.max_model_tokens == 10_000


@pytest.mark.asyncio
async def test_skill_packages_are_never_imported_as_python_tools(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    skill_dir = _write_skill(
        skills_root,
        "markdown_only",
        name="markdown_only",
        description="Use when a markdown-only Skill is requested.",
    )
    (skill_dir / "__init__.py").write_text(
        "raise RuntimeError('Skill packages must never be imported')\n", encoding="utf-8"
    )

    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    assert "markdown_only" in {item["name"] for item in listed["skills"]}


@pytest.mark.asyncio
async def test_legacy_frontmatter_fields_cannot_hide_a_standard_skill(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    _write_skill(
        skills_root,
        "legacy_extensions",
        name="legacy_extensions",
        description="Use to verify standard discovery ignores legacy routing fields.",
        metadata="source: internal_helper\ntool_name: internal.hidden\n",
    )

    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    assert "legacy_extensions" in {item["name"] for item in listed["skills"]}


@pytest.mark.asyncio
async def test_explicit_skills_root_is_an_orchestrator_package(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "install-root" / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    _write_skill(
        skills_root,
        "bright_prompt",
        name="bright_prompt",
        description="Use for bright image prompts.",
        body="Keep the subject bright and clear.\n",
    )

    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    item = next(item for item in listed["skills"] if item["name"] == "bright_prompt")
    assert item["package"] == "user/bright_prompt"
    loaded = _value(
        await skill_tools.skills_read(item["authority"], item["package"], "SKILL.md")
    )
    assert "bright and clear" in loaded["contents"]


@pytest.mark.asyncio
async def test_skills_read_reads_relative_resources_and_blocks_escape(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    skill_dir = _write_skill(
        skills_root,
        "resourceful",
        name="resourceful",
        description="Use when advanced reference instructions are needed.",
        body="Read references/advanced.md when needed.\n",
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "advanced.md").write_text("# Advanced\n\nRead only this rule.\n", encoding="utf-8")

    loaded = _value(
        await skill_tools.skills_read(
            {"kind": "orchestrator"}, "user/resourceful", "references/advanced.md"
        )
    )
    assert loaded["resource"] == "references/advanced.md"
    assert "Read only this rule" in loaded["contents"]

    escaped = _value(
        await skill_tools.skills_read(
            {"kind": "orchestrator"}, "user/resourceful", "../outside.md"
        )
    )
    assert escaped["ok"] is False
    assert escaped["error_kind"] == "invalid_resource"


@pytest.mark.asyncio
async def test_invalid_standard_skill_is_omitted_and_reported_as_warning(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    legacy = skills_root / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text("# not a package\n", encoding="utf-8")
    invalid = skills_root / "invalid"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text("---\nname: invalid\n---\n", encoding="utf-8")
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    assert all(item["name"] not in {"legacy", "invalid"} for item in listed["skills"])
    assert any("missing field `description`" in warning for warning in listed["warnings"])


@pytest.mark.asyncio
async def test_allow_implicit_false_hides_catalog_but_explicit_path_still_loads(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = _write_skill(
        skills_root,
        "explicit_only",
        name="explicit_only",
        description="Build for AWS: ECS deployment",
        body="Only load this Skill explicitly.\n",
    )
    metadata_dir = skill_dir / "agents"
    metadata_dir.mkdir()
    (metadata_dir / "openai.yaml").write_text(
        "interface:\n  display_name: Explicit only\n"
        "policy:\n  allow_implicit_invocation: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))

    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    assert "explicit_only" not in {item["name"] for item in listed["skills"]}
    assert "explicit_only" not in skill_tools.render_available_skills_context()

    locator = "skill://user/explicit_only/SKILL.md"
    injected = skill_tools.build_explicit_skill_injections(
        f"Use [$explicit_only]({locator})."
    )
    assert injected["selected_names"] == ("explicit_only",)
    assert f"<path>{locator}</path>" in injected["instructions"][0]
    assert "Only load this Skill explicitly" in injected["instructions"][0]


def test_explicit_selection_matches_codex_order_and_path_rules(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    _write_skill(skills_root, "one", name="one", description="First Skill.")
    _write_skill(skills_root, "two", name="two", description="Second Skill.")

    mentions = skill_tools.extract_explicit_skill_mentions(
        "$two and [$one](skill://user/one/SKILL.md), not $HOME.",
        attachments=[
            {
                "kind": "skill",
                "name": "one",
                "path": "skill://user/one/SKILL.md",
            }
        ],
    )
    assert mentions[0]["kind"] == "structured"
    injected = skill_tools.build_explicit_skill_injections(
        "$two",
        attachments=[
            {
                "kind": "skill",
                "name": "one",
                "path": "skill://user/one/SKILL.md",
            }
        ],
    )
    assert injected["selected_names"] == ("one", "two")

    name_only_structured = skill_tools.build_explicit_skill_injections(
        "", attachments=[{"kind": "skill", "name": "one"}]
    )
    assert name_only_structured["instructions"] == ()

    blocked_fallback = skill_tools.build_explicit_skill_injections(
        "$one",
        attachments=[
            {"kind": "skill", "name": "one", "path": "skill://user/missing/SKILL.md"}
        ],
    )
    assert blocked_fallback["instructions"] == ()


def test_plain_explicit_name_must_be_unambiguous(tmp_path, monkeypatch) -> None:
    user_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(user_root))
    _write_skill(user_root, "a", name="duplicate", description="User duplicate.")
    builtin_root = tmp_path / "builtin"
    _write_skill(builtin_root, "b", name="duplicate", description="Builtin duplicate.")
    monkeypatch.setattr(skill_tools, "_BUILTIN_SKILLS_ROOT", builtin_root)

    assert skill_tools.build_explicit_skill_injections("$duplicate")["instructions"] == ()
    linked = skill_tools.build_explicit_skill_injections(
        "[$duplicate](skill://user/a/SKILL.md)"
    )
    assert linked["selected_names"] == ("duplicate",)


def test_explicit_prompt_uses_codex_utf8_byte_limit(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    _write_skill(
        skills_root,
        "large_skill",
        name="large_skill",
        description="A large explicit Skill.",
        body="规则" * 5_000,
    )

    injected = skill_tools.build_explicit_skill_injections("$large_skill")
    contents = injected["instructions"][0].split("\n", 3)[3].rsplit("\n</skill>", 1)[0]
    assert len(contents.encode("utf-8")) <= skill_tools.MAX_EXPLICIT_SKILL_PROMPT_BYTES
    assert "skills.read" in injected["warnings"][0]
    assert "locator's exact package" in injected["warnings"][0]


@pytest.mark.asyncio
async def test_skills_list_uses_fingerprinted_cursor_and_rejects_stale_cursor(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    monkeypatch.setattr(skill_tools, "_MAX_SKILLS_PER_PAGE", 1)
    _write_skill(skills_root, "one", name="one", description="First Skill.")
    _write_skill(skills_root, "two", name="two", description="Second Skill.")

    first = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    assert len(first["skills"]) == 1
    assert first["next_cursor"]
    second = _value(
        await skill_tools.skills_list(
            {"kind": "orchestrator"}, cursor=first["next_cursor"]
        )
    )
    assert len(second["skills"]) == 1

    _write_skill(skills_root, "three", name="three", description="Third Skill.")
    stale = _value(
        await skill_tools.skills_list(
            {"kind": "orchestrator"}, cursor=first["next_cursor"]
        )
    )
    assert stale["error_kind"] == "stale_cursor"


@pytest.mark.asyncio
async def test_skills_read_uses_utf8_byte_cursor_and_rejects_stale_cursor(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    monkeypatch.setattr(skill_tools, "_MAX_READ_CONTENT_BYTES", 17)
    skill_dir = _write_skill(
        skills_root,
        "paged",
        name="paged",
        description="A paged Skill.",
        body="中文内容" * 30,
    )

    first = _value(
        await skill_tools.skills_read(
            {"kind": "orchestrator"}, "user/paged", "SKILL.md"
        )
    )
    assert first["next_cursor"]
    second = _value(
        await skill_tools.skills_read(
            {"kind": "orchestrator"},
            "user/paged",
            "SKILL.md",
            cursor=first["next_cursor"],
        )
    )
    assert second["contents"]

    (skill_dir / "SKILL.md").write_text(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8") + "changed",
        encoding="utf-8",
    )
    stale = _value(
        await skill_tools.skills_read(
            {"kind": "orchestrator"},
            "user/paged",
            "SKILL.md",
            cursor=first["next_cursor"],
        )
    )
    assert stale["error_kind"] == "stale_cursor"


@pytest.mark.asyncio
async def test_skills_read_enforces_codex_one_megabyte_resource_limit(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    skill_dir = _write_skill(
        skills_root,
        "oversized",
        name="oversized",
        description="Use to verify the resource size boundary.",
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "too-large.md").write_text(
        "x" * (skill_tools._MAX_SKILL_RESOURCE_CONTENT_BYTES + 1), encoding="utf-8"
    )

    loaded = _value(
        await skill_tools.skills_read(
            {"kind": "orchestrator"}, "user/oversized", "references/too-large.md"
        )
    )
    assert loaded["error_kind"] == "resource_too_large"


@pytest.mark.asyncio
async def test_executor_authority_is_empty_in_openreel(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(tmp_path / "skills"))
    listed = _value(await skill_tools.skills_list({"kind": "executor"}))
    assert listed == {"skills": [], "warnings": [], "next_cursor": None}
    loaded = _value(
        await skill_tools.skills_read(
            {"kind": "executor", "id": "worker"}, "pkg", "SKILL.md"
        )
    )
    assert loaded["error_kind"] == "package_not_available"


def test_runtime_catalog_keeps_all_locators_and_description_prefixes() -> None:
    catalog = skill_tools.render_available_skills_context()
    visible = [
        item
        for item in skill_tools._build_unified_index()
        if item.get("allow_implicit_invocation") is not False
    ]
    for item in visible:
        assert f"- {item['name']}:" in catalog
        assert f"(orchestrator resource: {item['locator']})" in catalog
        assert skill_tools._catalog_description(item)[:20] in catalog
    assert catalog.startswith("<skills_instructions>\n## Skills\n")
    assert catalog.endswith("\n</skills_instructions>")
    assert "### How to use skills" in catalog


def test_runtime_catalog_uses_skill_description_not_ui_short_description(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(skills_root))
    skill_dir = _write_skill(
        skills_root,
        "release_brief",
        name="release_brief",
        description="Use when the user asks to turn completed changes into release notes.",
    )
    metadata_dir = skill_dir / "agents"
    metadata_dir.mkdir()
    (metadata_dir / "openai.yaml").write_text(
        "interface:\n"
        '  display_name: "Release Brief"\n'
        '  short_description: "Short UI label that is not a trigger"\n',
        encoding="utf-8",
    )

    catalog = skill_tools.render_available_skills_context()
    assert "Use when the user asks to turn completed changes into release notes." in catalog
    assert "Short UI label that is not a trigger" not in catalog


def test_skills_usage_protocol_matches_codex_source_contract() -> None:
    # Hashes pin the exact upstream Codex constants without duplicating the
    # complete 3K usage protocol in the test body.
    assert hashlib.sha256(
        skill_tools.SKILLS_INTRO_WITH_ABSOLUTE_PATHS.encode("utf-8")
    ).hexdigest() == "46ccc2267a6792a99ae3025d6c8021b9ec0a490614c5cdd4fe1a0c47c36984e4"
    assert hashlib.sha256(
        skill_tools.SKILLS_HOW_TO_USE_WITH_ABSOLUTE_PATHS.encode("utf-8")
    ).hexdigest() == "acea851e76fd3c2fdffab880258fc9d63b88aeac003fe246ec21d2ab418a2ddb"
    assert skill_tools.DEFAULT_SKILL_METADATA_CHAR_BUDGET == 8_000


@pytest.mark.asyncio
async def test_builtin_prompt_and_review_skills_are_listed_without_semantic_search(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENREEL_SKILLS_DIR", str(tmp_path / "skills"))
    listed = _value(await skill_tools.skills_list({"kind": "orchestrator"}))
    names = {item["name"] for item in listed["skills"]}
    assert {
        "script_writing",
        "character_prompt",
        "scene_prompt",
        "shot_grid_prompt",
        "video_prompt",
        "storyboard_frame_check",
        "video_production",
    } <= names
