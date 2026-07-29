from __future__ import annotations

import csv
from html import escape
from pathlib import Path

from keysight_scope_app.services.test_profiles import TestRun


class ReportExporter:
    def export_html(self, run: TestRun, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(
            "<tr>"
            f"<td>{escape(metric.name)}</td><td>{escape(metric.status.value)}</td>"
            f"<td>{'' if metric.value is None else metric.value:g}</td><td>{escape(metric.unit)}</td>"
            f"<td>{escape(metric.reason)}</td></tr>"
            for metric in run.metrics
        )
        screenshot = (
            f'<p><img alt="关键波形" src="{escape(run.screenshot_path)}"></p>'
            if run.screenshot_path
            else ""
        )
        html = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>测试报告 {escape(run.sample_id)}</title>
<style>body{{font-family:sans-serif;max-width:1000px;margin:2rem auto}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:.5rem}}</style>
<h1>Keysight 示波器测试报告</h1>
<p>样品：{escape(run.sample_id)}　结论：<strong>{run.status.value}</strong></p>
<p>设备：{escape(run.instrument_id)}　方案：{escape(run.profile_name)} {escape(run.profile_version)}</p>
<p>运行 ID：{escape(run.run_id)}　时间：{escape(run.generated_at)}　软件：{escape(run.app_version)}</p>
<p>原始波形：{escape(run.waveform_path or "未记录")}</p>
<table><thead><tr><th>指标</th><th>判定</th><th>实测值</th><th>单位</th><th>原因</th></tr></thead>
<tbody>{rows}</tbody></table>{screenshot}</html>"""
        target_path.write_text(html, encoding="utf-8")
        return target_path

    def export_csv(self, runs: list[TestRun] | tuple[TestRun, ...], target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["run_id", "sample_id", "status", "profile", "profile_version", "instrument_id",
                 "generated_at", "metric", "metric_status", "value", "unit", "reason", "waveform_path"]
            )
            for run in runs:
                metrics = run.metrics or (None,)
                for metric in metrics:
                    writer.writerow([
                        run.run_id, run.sample_id, run.status.value, run.profile_name, run.profile_version,
                        run.instrument_id, run.generated_at, "" if metric is None else metric.name,
                        "" if metric is None else metric.status.value,
                        "" if metric is None or metric.value is None else metric.value,
                        "" if metric is None else metric.unit, "" if metric is None else metric.reason,
                        run.waveform_path or "",
                    ])
        return target_path
