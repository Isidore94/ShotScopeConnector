"""Standalone packaging and isolation tests; no live account or Windows login."""
import json
from pathlib import Path
import pytest
from shotscope_connector import connector as mod


def test_init_creates_only_shotscope_settings(tmp_path):
    config = tmp_path / '.env'
    assert mod.main(['--env', str(config), 'init']) == 0
    text = config.read_text()
    assert 'SHOTSCOPE_DATA_DIR=' in text
    assert 'SHOTSCOPE_EMAIL=' in text
    assert 'PASSWORD=' not in text and 'UPLOAD_TOKEN=' not in text
    assert not (tmp_path / 'square.sqlite3').exists()


def test_init_refuses_to_overwrite_existing_configuration(tmp_path):
    config = tmp_path / '.env'
    original = 'UPLOAD_TOKEN=synthetic-not-a-real-secret\nPORT=8790\n'
    config.write_text(original)
    assert mod.main(['--env', str(config), 'init']) == 2
    assert config.read_text() == original


def test_status_ignores_square_data_and_drive_settings(tmp_path, monkeypatch, capsys):
    local = tmp_path / 'new_shotscope'
    square = tmp_path / 'existing_square'
    monkeypatch.setenv('LOCAL_DATA_DIR', str(square))
    monkeypatch.setenv('DRIVE_OUTPUT_DIR', str(tmp_path / 'square_drive'))
    monkeypatch.setenv('SHOTSCOPE_DATA_DIR', str(local))
    monkeypatch.setenv('SHOTSCOPE_OUTPUT_DIR', '')
    monkeypatch.setenv('SHOTSCOPE_DISTANCE_UNIT', 'unknown')
    assert mod.main(['--env', str(tmp_path / 'absent.env'), 'status']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['rounds_stored'] == 0
    assert (local / 'shotscope.sqlite3').is_file()
    assert not square.exists()


def test_credential_namespace_is_independent():
    assert mod.KEYRING_SERVICE == 'ShotScopeConnector'


@pytest.mark.parametrize('marker', ['all_shots.csv', 'sessions.csv'])
def test_publisher_rejects_square_destination(tmp_path, marker):
    store = mod.Store(tmp_path / 'local')
    store.set('publication_pending', True)
    output = tmp_path / 'Square'
    output.mkdir()
    (output / marker).write_text('sentinel')
    with pytest.raises(mod.SyncError, match='Square outputs'):
        mod.publish(store, output)
    assert (output / marker).read_text() == 'sentinel'
    assert not (output / 'manifest.json').exists()


def test_publisher_rejects_foreign_manifest(tmp_path):
    store = mod.Store(tmp_path / 'local')
    store.set('publication_pending', True)
    output = tmp_path / 'output'
    output.mkdir()
    sentinel = '{"schema_version":"another-app-1"}'
    (output / 'manifest.json').write_text(sentinel)
    with pytest.raises(mod.SyncError, match='another app'):
        mod.publish(store, output)
    assert (output / 'manifest.json').read_text() == sentinel


@pytest.mark.parametrize('relation', ['same', 'parent', 'child'])
def test_publisher_rejects_local_output_overlap(tmp_path, relation):
    root = tmp_path / 'local'
    store = mod.Store(root)
    store.set('publication_pending', True)
    output = {'same': root, 'parent': tmp_path, 'child': root / 'out'}[relation]
    output.mkdir(exist_ok=True)
    with pytest.raises(mod.SyncError, match='overlap'):
        mod.publish(store, output)
