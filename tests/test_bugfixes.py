"""TDD 回归测试：覆盖 QQ 表结构固化、辅助库/通讯录映射、诊断兜底、向导配置保存与彻底卸载全链路。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402
import diagnose_collect  # noqa: E402
import probe  # noqa: E402
import qqdb  # noqa: E402
import qqnames  # noqa: E402
import server  # noqa: E402
import uninstaller  # noqa: E402
import wxmsg  # noqa: E402


class TestQQSchemaAndNames(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.execute('CREATE TABLE "msg_table" ("40001" INTEGER, "40050" INTEGER, "40800" BLOB)')
        self.con.execute("INSERT INTO \"msg_table\" VALUES (1, 1700000000, X'0102')")
        self.con.execute('CREATE TABLE "group_list" ("60001" TEXT, "60007" TEXT)')
        self.con.execute('CREATE TABLE "weird""name" ("col1" TEXT)')
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()

    def test_qqdb_columns_and_row_count_work_without_name_error(self) -> None:
        cols = qqdb.columns(self.con, "msg_table")
        self.assertEqual(cols, ["40001", "40050", "40800"])
        cnt = qqdb.row_count(self.con, "msg_table")
        self.assertEqual(cnt, 1)
        self.assertEqual(qqdb.columns(self.con, 'weird"name'), ["col1"])

    def test_qqnames_has_col_detects_existing_columns(self) -> None:
        self.assertTrue(qqnames._has_col(self.con, "group_list", "60001"))
        self.assertTrue(qqnames._has_col(self.con, "group_list", "60007"))
        self.assertFalse(qqnames._has_col(self.con, "group_list", "99999"))

    def test_wxmsg_namebook_loads_contact_names_without_name_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mirror = Path(td)
            contact_dir = mirror / "contact"
            contact_dir.mkdir(parents=True)
            db = contact_dir / "contact.db"
            con = sqlite3.connect(str(db))
            con.execute('CREATE TABLE "contact" ("username" TEXT, "remark" TEXT, "nick_name" TEXT)')
            con.execute('INSERT INTO "contact" VALUES (?, ?, ?)', ("wxid_alice", "张主任", "Alice"))
            con.commit()
            con.close()

            book = wxmsg.NameBook(mirror)
            self.assertEqual(book.resolve("wxid_alice"), "张主任")

    def test_probe_verify_decrypt_and_dump_schema_end_to_end(self) -> None:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            raw_sqlcipher = base / "plain_cipher.db"
            nt_db = base / "nt_msg.db"
            key = "Abcd1234Efgh5678"

            con = sqlcipher.connect(str(raw_sqlcipher))
            con.execute(f"PRAGMA key = '{key}';")
            con.execute("PRAGMA cipher_page_size = 4096;")
            con.execute("PRAGMA kdf_iter = 4000;")
            con.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
            con.execute(
                'CREATE TABLE "group_msg_table" '
                '("40001" INTEGER PRIMARY KEY, "40050" INTEGER, "40027" INTEGER, '
                '"40020" TEXT, "40800" TEXT)'
            )
            now_ts = int(time.time())
            for i in range(5):
                con.execute(
                    'INSERT INTO "group_msg_table" VALUES (?, ?, ?, ?, ?)',
                    (10000 + i, now_ts - i * 10, 1073282949, "u_sender1", f"请本周五前提交工作总结第{i}份"),
                )
            con.commit()
            con.close()

            body = raw_sqlcipher.read_bytes()
            header = b"\x00" * qqdb.QQ_HEADER
            nt_db.write_bytes(header + body)

            tmp_db = base / "tmp_db"
            out_schema = base / "schema.json"
            out_txt = base / "schema.txt"
            with (
                mock.patch.object(common, "TMP_DB", tmp_db),
                mock.patch.object(common, "WORK", base),
            ):
                schema = probe.verify_decrypt_and_dump_schema(
                    nt_db, key, "ntqq_official", out_schema=out_schema, dump_txt=out_txt
                )

            self.assertTrue(out_schema.exists())
            self.assertTrue(out_txt.exists())
            self.assertTrue(schema.get("tables"))
            self.assertEqual(schema["tables"][0]["name"], "group_msg_table")

    def test_diagnose_collect_does_not_crash_when_qq_db_path_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work_dir = base / "work"
            out_dir = base / "output"
            cfg_dir = base / "config"
            work_dir.mkdir()
            out_dir.mkdir()
            cfg_dir.mkdir()
            env_file = base / ".env"
            env_file.write_text("QQ_TARGET_UIN=\nQQ_DB_PATH=\n", encoding="utf-8")

            with (
                mock.patch.object(common, "WORK", work_dir),
                mock.patch.object(common, "OUT", out_dir),
                mock.patch.object(common, "CONFIG", cfg_dir),
                mock.patch.object(common, "ENV_PATH", env_file),
                mock.patch.object(common, "SCHEMA_PATH", cfg_dir / "schema.json"),
                mock.patch.object(common, "MATERIAL_PATH", work_dir / "待办素材.json"),
                mock.patch.object(common, "CURSOR_PATH", work_dir / "last_sync_ts.txt"),
            ):
                report = diagnose_collect.collect(deep=False)
                self.assertIn("problems", report)
                self.assertEqual(report["verdict"], "C_no_source")

    def test_wizard_step3_payload_matches_server_config_api(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        start = html.index("async function wzSaveStep3Config()")
        end = html.index("async function wzRefreshInspectSummary()")
        fn_body = html[start:end]
        self.assertIn("config_mode:", fn_body)
        self.assertIn("params:", fn_body)
        self.assertNotIn("env_updates:", fn_body)
        self.assertNotIn("wecomHook !== undefined", fn_body)

    def test_qq_extract_key_endpoint_auto_repairs_missing_schema_when_key_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg_dir = base / "config"
            cfg_dir.mkdir()
            db_file = base / "nt_msg.db"
            db_file.write_bytes(b"placeholder")
            schema_file = cfg_dir / "schema.json"

            httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.ProcessConsoleHTTPHandler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                def fake_dump(target_db, key, preferred):
                    schema_file.write_text(json.dumps({"tables": [{"name": "group_msg_table"}]}), encoding="utf-8")
                    return {"tables": [{"name": "group_msg_table"}]}

                with (
                    mock.patch.object(server, "CONFIG_DIR", cfg_dir),
                    mock.patch.object(common, "SCHEMA_PATH", schema_file),
                    mock.patch.object(
                        server,
                        "load_env_dict",
                        return_value={
                            "QQ_DBKEY": "Abcd1234Efgh5678",
                            "QQ_DB_PATH": str(db_file),
                            "QQ_CIPHER_PROFILE": "ntqq_official",
                        },
                    ),
                    mock.patch("runtime_env.has_all", return_value=True),
                    mock.patch("probe.verify_decrypt_and_dump_schema", side_effect=fake_dump) as mock_dump,
                    mock.patch("health.trigger_qq_key_extractor", return_value={"ok": False}) as mock_trigger,
                ):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/qq/extract-key",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.assertEqual(resp.status, 200)
                        body = json.loads(resp.read().decode("utf-8"))
                        self.assertTrue(body["ok"])
                        self.assertTrue(body["schema_ready"])
                        self.assertEqual(body["action"], "schema_repaired")
                    mock_dump.assert_called_once()
                    mock_trigger.assert_not_called()
            finally:
                httpd.shutdown()
                httpd.server_close()


class TestUninstallerAndEndpoint(unittest.TestCase):
    def test_stop_processes_ignores_dead_stale_pid_and_checks_console_port(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work_dir = base / "work"
            work_dir.mkdir()
            (work_dir / "scheduler.pid").write_text("999998", encoding="utf-8")
            (work_dir / "console_port.json").write_text(json.dumps({"pid": 999997, "port": 8000}), encoding="utf-8")

            seen_taskkill_pids: list[str] = []

            def fake_run(cmd, **kwargs):
                if cmd and cmd[0] == "powershell":
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd and cmd[0] == "taskkill":
                    pid_arg = cmd[-1]
                    seen_taskkill_pids.append(pid_arg)
                    return subprocess.CompletedProcess(
                        cmd, 128, stdout="", stderr=f'ERROR: The process "{pid_arg}" not found.'
                    )
                if cmd and cmd[0] == "tasklist":
                    return subprocess.CompletedProcess(cmd, 0, stdout="INFO: No tasks are running.", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                mock.patch.object(common, "WORK", work_dir),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                res = uninstaller.stop_processes()
                self.assertTrue(res["ok"], f"stale PID should not fail uninstall: {res}")
                self.assertEqual(res["failed"], [])
                self.assertIn("999997", seen_taskkill_pids)
                self.assertIn("999998", seen_taskkill_pids)
                self.assertFalse((work_dir / "scheduler.pid").exists())

    def test_locked_app_profile_not_falsely_reported_clean_without_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work_dir = base / "work"
            out_dir = base / "output"
            cfg_dir = base / "config"
            work_dir.mkdir()
            out_dir.mkdir()
            cfg_dir.mkdir()
            profile_dir = work_dir / ".app_profile"
            profile_dir.mkdir()
            locked_file = profile_dir / "LOCK"
            locked_file.write_text("locked", encoding="utf-8")

            with (
                mock.patch.object(common, "WORK", work_dir),
                mock.patch.object(common, "OUT", out_dir),
                mock.patch.object(common, "CONFIG", cfg_dir),
                mock.patch.object(common, "ENV_PATH", base / ".env"),
                mock.patch.object(common, "ROOT", base),
                mock.patch.object(uninstaller, "remove_autostart", return_value={"ok": True}),
                mock.patch.object(uninstaller, "remove_shortcuts", return_value=[]),
                mock.patch.object(uninstaller, "restore_qq_updates", return_value={"ok": True}),
                mock.patch("shutil.rmtree", side_effect=PermissionError("in use")),
            ):
                res = uninstaller.full_uninstall(
                    purge_env=True,
                    kill_processes=False,
                    schedule_deferred=False,
                )
                self.assertFalse(res["ok"])
                self.assertTrue(any(".app_profile" in p for p in res["problems"]))

    def test_locked_app_profile_schedules_deferred_cleanup_script(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work_dir = base / "work"
            out_dir = base / "output"
            cfg_dir = base / "config"
            work_dir.mkdir()
            out_dir.mkdir()
            cfg_dir.mkdir()
            profile_dir = work_dir / ".app_profile"
            profile_dir.mkdir()
            (profile_dir / "LOCK").write_text("locked", encoding="utf-8")
            server_log = work_dir / "server.log"
            server_log.write_text("log", encoding="utf-8")

            spawned_cmds: list[list[str]] = []
            orig_unlink = Path.unlink

            def fake_unlink(self_path: Path, missing_ok: bool = False):
                if self_path.name == "server.log":
                    raise PermissionError("locked by running server")
                return orig_unlink(self_path, missing_ok=missing_ok)

            def fake_popen(cmd, **kwargs):
                spawned_cmds.append(list(cmd))
                return mock.MagicMock()

            with (
                mock.patch.object(common, "WORK", work_dir),
                mock.patch.object(common, "OUT", out_dir),
                mock.patch.object(common, "CONFIG", cfg_dir),
                mock.patch.object(common, "ENV_PATH", base / ".env"),
                mock.patch.object(common, "ROOT", base),
                mock.patch.object(uninstaller, "remove_autostart", return_value={"ok": True}),
                mock.patch.object(uninstaller, "remove_shortcuts", return_value=[]),
                mock.patch.object(uninstaller, "restore_qq_updates", return_value={"ok": True}),
                mock.patch("shutil.rmtree", side_effect=PermissionError("in use")),
                mock.patch.object(Path, "unlink", autospec=True, side_effect=fake_unlink),
                mock.patch("subprocess.Popen", side_effect=fake_popen),
            ):
                res = uninstaller.full_uninstall(
                    purge_env=True,
                    kill_processes=False,
                    schedule_deferred=True,
                )
                self.assertTrue(res["ok"])
                self.assertTrue(res.get("deferred_cleanup"))
                self.assertEqual(len(spawned_cmds), 1)
                ps1_path = Path(spawned_cmds[0][-1])
                self.assertTrue(ps1_path.exists())
                self.assertEqual(ps1_path.suffix, ".ps1")
                # 延迟脚本应落在系统 TEMP，避免残留在 work/ 目录内
                self.assertNotEqual(ps1_path.parent.resolve(), work_dir.resolve())
                ps1_text = ps1_path.read_text(encoding="utf-8-sig")
                self.assertIn(".app_profile", ps1_text)
                self.assertIn("server.log", ps1_text)
                # 应包含针对隔离浏览器配置进程与重试清理的逻辑
                self.assertIn("Win32_Process", ps1_text)
                orig_unlink(ps1_path, missing_ok=True)

    def test_deferred_cleanup_script_executes_and_deletes_chinese_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cn_root = Path(td) / "测试待办目录" / "work"
            prof = cn_root / ".app_profile"
            prof.mkdir(parents=True)
            (prof / "LOCK").write_text("locked", encoding="utf-8")
            srv_log = cn_root / "server.log"
            srv_log.write_text("log", encoding="utf-8")

            script_str = uninstaller._schedule_deferred_cleanup([prof], [srv_log], delay_sec=0)
            self.assertIsNotNone(script_str)
            script_path = Path(script_str)
            deadline = time.time() + 15.0
            while time.time() < deadline and (prof.exists() or srv_log.exists() or script_path.exists()):
                time.sleep(0.2)
            self.assertFalse(prof.exists(), ".app_profile under Chinese path should be deleted")
            self.assertFalse(srv_log.exists(), "server.log under Chinese path should be deleted")
            self.assertFalse(script_path.exists(), "deferred cleanup script should self-delete")

    def test_full_uninstall_cleans_schema_outputs_inbox_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work_dir = base / "work"
            out_dir = base / "output"
            cfg_dir = base / "config"
            inbox_dir = work_dir / "inbox"
            work_dir.mkdir()
            out_dir.mkdir()
            cfg_dir.mkdir()
            inbox_dir.mkdir()

            (base / ".env").write_text("QQ_DBKEY=secret\n", encoding="utf-8")
            (cfg_dir / "schema.json").write_text('{"tables": []}', encoding="utf-8")
            (cfg_dir / "wizard_state.json").write_text('{"completed": true}', encoding="utf-8")
            (cfg_dir / "active_platforms.json").write_text(
                '{"config_mode": "custom", "enabled_platforms": ["QQ"]}', encoding="utf-8"
            )
            (work_dir / "console_port.json").write_text('{"port": 8000}', encoding="utf-8")
            (work_dir / "channel_cursors.json").write_text('{"QQ": 1}', encoding="utf-8")
            (work_dir / "取密钥日志-20261125-1800.txt").write_text("log", encoding="utf-8")
            (work_dir / "schema.txt").write_text("schema", encoding="utf-8")
            (work_dir / "group_names.json").write_text("{}", encoding="utf-8")
            (inbox_dir / "wecom.jsonl").write_text('{"content": "hi"}\n', encoding="utf-8")
            (out_dir / "todo-20261125-1801.md").write_text("# todo", encoding="utf-8")
            (out_dir / "异常说明-密钥未就绪.txt").write_text("err", encoding="utf-8")
            (out_dir / "采集诊断-20261125.txt").write_text("diag", encoding="utf-8")

            with (
                mock.patch.object(common, "WORK", work_dir),
                mock.patch.object(common, "OUT", out_dir),
                mock.patch.object(common, "CONFIG", cfg_dir),
                mock.patch.object(common, "ENV_PATH", base / ".env"),
                mock.patch.object(common, "ROOT", base),
                mock.patch.object(uninstaller, "remove_autostart", return_value={"ok": True}),
                mock.patch.object(uninstaller, "remove_shortcuts", return_value=[]),
                mock.patch.object(uninstaller, "restore_qq_updates", return_value={"ok": True}),
            ):
                res = uninstaller.full_uninstall(purge_env=True, kill_processes=False)
                self.assertTrue(res["ok"], res)
                self.assertFalse((cfg_dir / "schema.json").exists())
                self.assertFalse((work_dir / "console_port.json").exists())
                self.assertFalse((work_dir / "channel_cursors.json").exists())
                self.assertFalse((work_dir / "取密钥日志-20261125-1800.txt").exists())
                self.assertFalse((work_dir / "schema.txt").exists())
                self.assertFalse((work_dir / "group_names.json").exists())
                self.assertFalse((inbox_dir / "wecom.jsonl").exists())
                self.assertFalse((out_dir / "todo-20261125-1801.md").exists())
                self.assertFalse((out_dir / "异常说明-密钥未就绪.txt").exists())
                self.assertFalse((out_dir / "采集诊断-20261125.txt").exists())
                active_reset = json.loads((cfg_dir / "active_platforms.json").read_text(encoding="utf-8"))
                self.assertEqual(active_reset["config_mode"], "")
                self.assertEqual(active_reset["enabled_platforms"], [])

    def test_uninstall_api_returns_json_before_exiting(self) -> None:
        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.ProcessConsoleHTTPHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with (
                mock.patch(
                    "uninstaller.full_uninstall",
                    return_value={"ok": True, "message": "done", "deferred_cleanup": True},
                ),
                mock.patch("os._exit") as mock_exit,
            ):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/app/uninstall",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(body["status"], "success")
                    self.assertTrue(body["data"]["ok"])
                time.sleep(0.1)
                mock_exit.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_uninstall_api_does_not_exit_when_uninstall_incomplete(self) -> None:
        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.ProcessConsoleHTTPHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with (
                mock.patch(
                    "uninstaller.full_uninstall",
                    return_value={"ok": False, "message": "partial", "problems": ["locked"]},
                ),
                mock.patch.object(server, "_schedule_uninstall_exit") as mock_sched_exit,
            ):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/app/uninstall",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(body["status"], "success")
                    self.assertFalse(body["data"]["ok"])
                mock_sched_exit.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_frontend_uninstall_stops_polling_and_uses_safe_timeout(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        start = html.index("async function confirmUninstallApp()")
        end = html.index("async function createDesktopShortcut()")
        fn_body = html[start:end]
        self.assertIn("timeout: 60000", fn_body)
        self.assertIn("pollTimers", fn_body)
        self.assertIn("window.close", fn_body)

    def test_onboarding_guides_warn_untested_feishu_dingtalk_and_settings_confirm(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for doc_name, text in (("index.html", html), ("README.md", readme)):
            self.assertIn("尚未经过", text, f"{doc_name} should note Feishu/DingTalk untested status")
            self.assertIn("飞书", text, f"{doc_name} should mention Feishu")
            self.assertIn("钉钉", text, f"{doc_name} should mention DingTalk")
            self.assertIn("系统设置", text, f"{doc_name} should direct users to confirm config in System Settings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
