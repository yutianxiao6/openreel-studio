from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_windows_packaging_waits_for_windowless_api_smoke_test() -> None:
    script = (PROJECT_ROOT / "scripts" / "desktop" / "build-windows.ps1").read_text(
        encoding="utf-8"
    )

    smoke_start = script.index('$env:OPENREEL_PACKAGING_SMOKE = "1"')
    catalog_check = script.index("foreach ($ProtocolDirName", smoke_start)
    smoke_block = script[smoke_start:catalog_check]

    assert "Start-Process" in smoke_block
    assert "-Wait" in smoke_block
    assert "-PassThru" in smoke_block
    assert "$SmokeProcess.ExitCode" in smoke_block
    assert "Invoke-Native" not in smoke_block
