#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tracelog_viewer.py — trace_log(id, data) 形式トレースログビューワ

タスク等から trace_log(id, data); で記録された固定長バイナリログを、
JSON で記述した ID テーブルに従ってデコードし、
  * CSV
  * HTML タイムラインビューワ
      - 上→下に時系列、レーン(タスク別など)ごとの列
      - 発生間隔を縦間隔に反映 (log スケール)。桁違いの間隔は省略線で圧縮
      - ID ごとにメッセージ文字列・data の解釈 (型/スケール/enum) を定義
を出力する。Python 3.8+ 標準ライブラリのみで動作。

使い方:
    python3 tracelog_viewer.py <logfile> -f format.json [-c out.csv] [-o out.html]

フォーマット定義の例は sample_trace_format.json を参照。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------- 基本型

TYPE_TABLE = {
    "int8": ("b", 1), "uint8": ("B", 1),
    "int16": ("h", 2), "uint16": ("H", 2),
    "int32": ("i", 4), "uint32": ("I", 4),
    "int64": ("q", 8), "uint64": ("Q", 8),
    "float32": ("f", 4), "float64": ("d", 8),
    "bool": ("B", 1),
}

UNIT_NS = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000}

LEVEL_COLOR = {"debug": "#7a8792", "info": "#3a6ea5",
               "warn": "#c25e00", "error": "#c22f4f"}
LEVEL_ORDER = {"debug": 0, "info": 1, "warn": 2, "error": 3}


class FormatError(Exception):
    pass


# ---------------------------------------------------------------- フィールド処理

def resolve_fields(fields, ctx):
    cursor = 0
    for f in fields:
        t = f.get("type")
        if t in TYPE_TABLE:
            size = TYPE_TABLE[t][1]
        elif t in ("char", "bytes", "pad"):
            size = int(f.get("length", 0))
            if size <= 0:
                raise FormatError(f"{ctx}: type={t} には length が必要です")
        else:
            raise FormatError(f"{ctx}: 未知の type \"{t}\"")
        off = int(f.get("offset", cursor))
        f["_offset"], f["_size"] = off, size
        cursor = max(cursor, off + size)
        if t != "pad" and not f.get("name"):
            raise FormatError(f"{ctx}: pad 以外のフィールドには name が必要です")
    return cursor


def _apply_map(raw, spec_):
    m = spec_.get("map")
    if m is None:
        return raw, False
    key = str(raw)
    if key in m:
        return m[key], True
    default = spec_.get("map_default")
    if default is not None:
        return str(default).replace("{value}", str(raw)), True
    return raw, False


def _format_display(value, spec_):
    disp = spec_.get("display")
    try:
        if disp == "hex":
            return f"0x{int(value):X}"
        if disp == "bin":
            return f"0b{int(value):b}"
    except (TypeError, ValueError):
        pass
    return value


def _transform_number(raw, spec_):
    val = raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if "scale" in spec_:
            val = raw * spec_["scale"]
        if "add" in spec_:
            val = val + spec_["add"]
        if isinstance(val, float):
            val = round(val, 6)
    disp, mapped = _apply_map(val, spec_)
    if not mapped:
        disp = _format_display(val, spec_)
    if disp == val and spec_.get("unit") and isinstance(val, (int, float)):
        pass  # 単位は表示側で付与
    return val, disp


def decode_fields(buf, fields, prefix):
    out = {}
    for f in fields:
        t, off = f["type"], f["_offset"]
        if t == "pad":
            continue
        if t == "char":
            text = buf[off:off + f["_size"]].split(b"\x00", 1)[0] \
                .decode(f.get("encoding", "utf-8"), errors="replace")
            out[f["name"]] = (text, text)
            continue
        if t == "bytes":
            hexstr = buf[off:off + f["_size"]].hex(" ").upper()
            out[f["name"]] = (hexstr, hexstr)
            continue
        fmt, _ = TYPE_TABLE[t]
        raw = struct.unpack_from(prefix + fmt, buf, off)[0]
        if t == "bool":
            raw = bool(raw)
        for b in f.get("bits", []):
            bs, bl = int(b["start"]), int(b.get("length", 1))
            bval = (int(raw) >> bs) & ((1 << bl) - 1)
            bdisp, _ = _apply_map(bval, b)
            out[b["name"]] = (bval, _format_display(bdisp, b))
        val, disp = _transform_number(raw, f)
        out[f["name"]] = (val, disp)
    return out


# ---------------------------------------------------------------- フォーマット読込

def load_format(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FormatError(f"フォーマット JSON の構文エラー: {e}") from e

    for key in ("fields", "timestamp", "id", "ids"):
        if key not in spec:
            raise FormatError(f'フォーマットに "{key}" が必要です')

    endian = spec.get("endian", "little")
    if endian not in ("little", "big"):
        raise FormatError('endian は "little" か "big"')
    spec["_prefix"] = "<" if endian == "little" else ">"

    need = resolve_fields(spec["fields"], "fields")
    spec["_record_size"] = int(spec.get("record_size", need))
    if spec["_record_size"] < need:
        raise FormatError(
            f"record_size ({spec['_record_size']}) が必要長 ({need}) より小さいです")

    fmap = {f.get("name"): f for f in spec["fields"] if f.get("name")}
    ts = spec["timestamp"]
    unit = ts.get("unit", "us")
    if unit not in UNIT_NS and "scale_ns" not in ts:
        raise FormatError('timestamp.unit は ns/us/ms/s、または scale_ns を指定')
    ts["_mult_ns"] = float(ts.get("scale_ns", UNIT_NS.get(unit, 1)))
    ts.setdefault("epoch", "relative")
    if ts.get("field") not in fmap:
        raise FormatError(f'timestamp.field "{ts.get("field")}" が fields にありません')

    if spec["id"].get("field") not in fmap:
        raise FormatError('id.field が fields にありません')

    df = spec.get("data_field")
    if df is not None and df not in fmap:
        raise FormatError(f'data_field "{df}" が fields にありません')
    spec["_data_off"] = fmap[df]["_offset"] if df else None

    lane_cfg = spec.get("lane", {})
    lane_field = lane_cfg.get("field")
    if lane_field is not None and lane_field not in fmap:
        raise FormatError(f'lane.field "{lane_field}" が fields にありません')

    for id_key, ent in spec["ids"].items():
        if id_key == "default":
            continue
        if not isinstance(ent, dict) or "name" not in ent:
            raise FormatError(f'ids["{id_key}"] に name が必要です')
        lv = ent.get("level", "info")
        if lv not in LEVEL_COLOR:
            raise FormatError(f'ids["{id_key}"].level は debug/info/warn/error')
    return spec


# ---------------------------------------------------------------- デコード

def decode_data(buf, id_ent, default_val, spec):
    """ID ごとの data 解釈。戻り値 (raw, 表示値, unit)"""
    dcfg = id_ent.get("data")
    if dcfg is None:
        return default_val[0] if default_val else None, \
               default_val[1] if default_val else None, ""
    off = spec["_data_off"]
    t = dcfg.get("type")
    if t is not None and off is not None:
        if t not in TYPE_TABLE:
            raise FormatError(f'data.type "{t}" は数値型のみ指定できます')
        fmt, _ = TYPE_TABLE[t]
        raw = struct.unpack_from(spec["_prefix"] + fmt, buf, off)[0]
    else:
        raw = default_val[0] if default_val else None
    val, disp = _transform_number(raw, dcfg)
    return val, disp, dcfg.get("unit", "")


def render_text(template, data_disp, unit, common):
    if template is None:
        return ""
    s = template.replace("{data}", ("" if data_disp is None else str(data_disp))
                         + (f" {unit}" if unit and data_disp is not None else ""))
    for k, v in common.items():
        s = s.replace("{" + k + "}", str(v[1]))
    return s


def decode_log(data: bytes, spec: dict, limit=None):
    rsize = spec["_record_size"]
    head = int(spec.get("header_bytes", 0))
    body = data[head:]
    n = len(body) // rsize
    remainder = len(body) % rsize
    if limit is not None:
        n = min(n, limit)

    prefix = spec["_prefix"]
    ts_cfg = spec["timestamp"]
    ts_field = ts_cfg["field"]
    id_field = spec["id"]["field"]
    data_field = spec.get("data_field")
    mult = ts_cfg["_mult_ns"]
    wrap_bits = ts_cfg.get("wrap_bits")
    lane_field = spec.get("lane", {}).get("field")
    default_ent = spec["ids"].get("default")

    entries = []
    unknown = {}
    wrap_add, prev_raw = 0, None
    for i in range(n):
        buf = body[i * rsize:(i + 1) * rsize]
        common = decode_fields(buf, spec["fields"], prefix)

        t_raw = common[ts_field][0]
        if wrap_bits:
            if prev_raw is not None and t_raw < prev_raw:
                wrap_add += 1 << int(wrap_bits)
            prev_raw = t_raw
            t_raw = t_raw + wrap_add
        if isinstance(t_raw, int) and float(mult).is_integer():
            t_ns = t_raw * int(mult)
        else:
            t_ns = int(round(float(t_raw) * mult))

        id_raw = common[id_field][0]
        ent = spec["ids"].get(str(id_raw))
        if ent is None:
            if default_ent is not None:
                ent = dict(default_ent)          # default を ID ごとに登録
            else:
                nm = f"ID_0x{int(id_raw):X}" if isinstance(id_raw, int) \
                     else f"ID_{id_raw}"
                ent = {"name": nm, "text": "data={data}", "_synthetic": True}
            ent["_undef"] = True
            spec["ids"][str(id_raw)] = ent
        if ent.get("_undef"):
            unknown[id_raw] = unknown.get(id_raw, 0) + 1

        d_raw, d_disp, d_unit = decode_data(
            buf, ent, common.get(data_field), spec)
        msg = render_text(ent.get("text"), d_disp, d_unit, common)

        lane = None
        if ent.get("lane") is not None:
            lane = str(ent["lane"])
        elif lane_field is not None:
            lane = str(common[lane_field][1])

        entries.append({
            "no": i + 1, "t_ns": t_ns, "id_key": str(id_raw),
            "common": common, "data_raw": d_raw, "data_disp": d_disp,
            "msg": msg, "lane": lane,
        })

    order_warn = any(entries[i]["t_ns"] < entries[i - 1]["t_ns"]
                     for i in range(1, len(entries)))
    entries.sort(key=lambda e: (e["t_ns"], e["no"]))
    return entries, remainder, unknown, order_warn


# ---------------------------------------------------------------- CSV

def write_csv(path, spec, entries, raw=False):
    idx = 0 if raw else 1
    ts_field = spec["timestamp"]["field"]
    id_field = spec["id"]["field"]
    data_field = spec.get("data_field")
    lane_field = spec.get("lane", {}).get("field")
    skip = {ts_field, id_field, data_field, lane_field}
    extra_cols = []
    for f in spec["fields"]:
        if f["type"] == "pad" or f.get("name") in skip:
            continue
        for b in f.get("bits", []):
            extra_cols.append(b["name"])
        if f.get("name"):
            extra_cols.append(f["name"])

    t0 = entries[0]["t_ns"] if entries else 0
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["record_no", "time_ns", "delta_ns", "id", "name",
                    "level", "lane", "message", "data"] + extra_cols)
        prev = t0
        for e in entries:
            ent = spec["ids"][e["id_key"]]
            row = [e["no"], e["t_ns"] - t0, e["t_ns"] - prev,
                   e["id_key"], ent["name"], ent.get("level", "info"),
                   e["lane"] or "", e["msg"],
                   e["data_raw"] if raw else e["data_disp"]]
            for c in extra_cols:
                row.append(e["common"][c][idx] if c in e["common"] else "")
            w.writerow(row)
            prev = e["t_ns"]


# ---------------------------------------------------------------- HTML

def build_html(spec, entries, meta):
    ts = spec["timestamp"]
    id_list, id_index = [], {}
    for key, ent in spec["ids"].items():
        if key == "default":
            continue
        lv = ent.get("level", "info")
        id_index[key] = len(id_list)
        id_list.append({
            "key": key, "name": ent["name"], "level": lv,
            "color": ent.get("color") or LEVEL_COLOR[lv],
        })

    t0_ns = entries[0]["t_ns"] if entries else 0
    t0_sec, t0_nsec = divmod(t0_ns, 1_000_000_000)

    ts_field = ts["field"]
    id_field = spec["id"]["field"]
    data_field = spec.get("data_field")
    rows = []
    for e in entries:
        detail = {}
        if e["data_disp"] is not None:
            detail["data"] = [e["data_raw"], e["data_disp"]]
        for k, v in e["common"].items():
            if k in (ts_field, id_field, data_field):
                continue
            detail[k] = [v[0], v[1]]
        rows.append({"n": e["no"], "t": e["t_ns"] - t0_ns,
                     "ii": id_index[e["id_key"]],
                     "lane": e["lane"], "msg": e["msg"], "d": detail})

    payload = {
        "title": spec.get("name", "Trace Log"),
        "meta": meta,
        "epoch": ts.get("epoch", "relative"),
        "t0Sec": t0_sec, "t0Nsec": t0_nsec,
        "ids": id_list,
        "events": rows,
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    return (HTML_TEMPLATE
            .replace("__TITLE__", html.escape(str(payload["title"])))
            .replace("__DATA__", data_json))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — Trace Log</title>
<style>
  :root{
    --paper:#eef1f4; --panel:#fff; --ink:#1b2733; --ink2:#5a6b7b;
    --grid:#d9dfe5; --lane:#e4e9ee;
    --mono:"SFMono-Regular","Consolas","BIZ UDGothic","Menlo",monospace;
    --sans:"Segoe UI","Hiragino Sans","Yu Gothic UI",system-ui,sans-serif;
    --gut:185px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:14px}
  header{background:var(--ink);color:#e8eef4;padding:18px 26px 14px;
         background-image:repeating-linear-gradient(90deg,transparent 0 118px,rgba(255,255,255,.05) 118px 120px)}
  header h1{margin:0;font-size:20px;font-weight:600;letter-spacing:.03em}
  header .sub{font-family:var(--mono);font-size:12px;color:#9fb3c6;margin-top:5px}
  main{max-width:1280px;margin:0 auto;padding:14px 22px 60px}

  .toolbar{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;position:sticky;top:0;z-index:6;
           background:var(--panel);border:1px solid var(--grid);padding:10px 14px;margin-bottom:10px;
           box-shadow:0 2px 6px rgba(27,39,51,.08)}
  .chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--grid);
        padding:3px 10px;cursor:pointer;user-select:none;background:var(--paper);
        font-size:12px;border-radius:2px}
  .chip .n{font-family:var(--mono);color:var(--ink2)}
  .chip.off{opacity:.35}
  .toolbar input[type=search]{padding:6px 10px;border:1px solid var(--grid);
        font-family:var(--mono);font-size:13px;min-width:190px}
  .toolbar label{font-size:12px;color:var(--ink2);display:inline-flex;gap:6px;align-items:center}
  .toolbar select{padding:4px 6px;border:1px solid var(--grid);background:#fff;font-size:12px}
  .toolbar .cnt{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink2)}

  details.idsum{margin-bottom:10px;border:1px solid var(--grid);background:var(--panel)}
  details.idsum summary{padding:7px 14px;font-size:12px;color:var(--ink2);cursor:pointer;user-select:none}
  .idgrid{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 12px}
  .idgrid .chip{font-family:var(--mono)}
  .idgrid .more{font-size:11px;color:var(--ink2);align-self:center}
  .lv{display:inline-block;width:8px;height:8px;border-radius:50%}

  .tl{background:var(--panel);border:1px solid var(--grid)}
  .lanehead{display:grid;position:sticky;top:56px;z-index:5;background:var(--ink);color:#e8eef4}
  .lanehead div{padding:8px 10px;font-size:12px;font-weight:600;letter-spacing:.05em;
                overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center}
  .lanehead div:first-child{text-align:left;font-family:var(--mono);color:#9fb3c6;font-weight:400}
  #tlBody{position:relative;overflow:hidden}
  .row{position:absolute;left:0;right:0;display:grid;align-items:center;height:26px}
  .gut{padding:0 12px 0 10px;text-align:right;font-family:var(--mono);
       font-size:11px;color:var(--ink2);line-height:1.15;border-right:1px solid var(--grid);
       height:100%;display:flex;flex-direction:column;justify-content:center}
  .gut .abs{color:var(--ink);white-space:nowrap;overflow:hidden}
  .gut .dlt{font-size:10px;color:#8b9aa8;white-space:nowrap;overflow:hidden}
  .cell{padding:0 6px;min-width:0;text-align:center}
  .evt{display:inline-block;max-width:100%;border:1px solid var(--grid);border-left-width:4px;
       background:#fff;padding:2px 9px;font-size:12px;cursor:pointer;border-radius:2px;
       box-shadow:0 1px 0 rgba(27,39,51,.06);
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:middle}
  .evt:hover{background:#f0f6fd}
  .evt.sel{outline:2px solid var(--ink)}
  .evt.lv-warn{background:#fff8ef}
  .evt.lv-error{background:#fdf1f3}
  .evt .nm{font-weight:600;letter-spacing:.02em;font-family:var(--mono);font-size:11px}
  .evt .msg{margin-left:7px}
  .break{position:absolute;left:0;right:0;display:flex;align-items:center;gap:12px;color:var(--ink2);
         font-family:var(--mono);font-size:11px;padding-right:14px;height:24px}
  .break::before{content:"";width:calc(var(--gut) - 14px);flex:none}
  .break .line{flex:1;border-top:2px dashed #b6c1cb}
  .break .lbl{flex:none;background:var(--paper);border:1px solid var(--grid);
              padding:2px 10px;border-radius:10px}
  #dpanel{position:fixed;right:18px;bottom:18px;width:min(440px,92vw);max-height:46vh;
          overflow:auto;background:#fff;border:1px solid var(--ink);z-index:20;display:none;
          box-shadow:0 8px 28px rgba(27,39,51,.28)}
  #dpanel .hd{display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;
              background:var(--ink);color:#e8eef4;padding:6px 12px;font-size:12px}
  #dpanel .hd button{background:none;border:none;color:#e8eef4;font-size:15px;cursor:pointer;padding:0 2px}
  #dpanel .bd{padding:8px 12px;font-family:var(--mono);font-size:12px}
  #dpanel table{border-collapse:collapse}
  #dpanel td{padding:2px 14px 2px 0;vertical-align:top}
  #dpanel td:first-child{color:var(--ink2)}
  .notice{padding:8px 14px;font-size:12px;color:#c25e00;
          background:#fff7ef;border:1px solid #f0d9bf;margin-bottom:10px}
  footer{max-width:1280px;margin:0 auto;padding:8px 22px 30px;color:var(--ink2);
         font-size:11px;font-family:var(--mono)}
  @media (max-width:700px){ :root{--gut:105px} .gut{font-size:10px} }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub" id="hMeta"></div>
</header>
<main>
  <div id="notices"></div>
  <div class="toolbar">
    <span id="laneChips"></span>
    <label>レベル
      <select id="lvSel">
        <option value="0" selected>すべて</option>
        <option value="1">info 以上</option>
        <option value="2">warn 以上</option>
        <option value="3">error のみ</option>
      </select>
    </label>
    <input type="search" id="q" placeholder="ID名・メッセージ検索…">
    <label>間隔
      <select id="mode">
        <option value="prop" selected>時間比例 (log)</option>
        <option value="uniform">等間隔</option>
      </select>
    </label>
    <label>省略線
      <select id="breakF">
        <option value="10">×10</option>
        <option value="100">×100</option>
        <option value="1000" selected>×1000</option>
        <option value="0">なし</option>
      </select>
    </label>
    <label>倍率
      <input type="range" id="zoom" min="4" max="40" value="14" style="width:100px">
    </label>
    <span class="cnt" id="cnt"></span>
  </div>
  <details class="idsum"><summary>ID 別件数(クリックで絞り込み)</summary>
    <div class="idgrid" id="idGrid"></div>
  </details>
  <div class="tl">
    <div class="lanehead" id="laneHead"></div>
    <div id="tlBody"></div>
  </div>
</main>
<div id="dpanel">
  <div class="hd"><span id="dpTitle"></span><button id="dpClose" title="閉じる">×</button></div>
  <div class="bd" id="dpBody"></div>
</div>
<footer id="foot"></footer>

<script id="logdata" type="application/json">__DATA__</script>
<script>
(function(){
"use strict";
const D = JSON.parse(document.getElementById("logdata").textContent);
const ids = D.ids, events = D.events, N = events.length;
const LV = {debug:0, info:1, warn:2, error:3};
const ROW_H = 26, BREAK_H = 24, BUF = 700;
const ID_CHIP_CAP = 200;

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

/* ---------- レーン構成・検索用文字列の前計算 ---------- */
const laneNames = [];
events.forEach(e=>{ const l = e.lane==null ? "LOG" : e.lane;
  e._lane = l; if(!laneNames.includes(l)) laneNames.push(l); });
const L = laneNames.length;
const laneIdx = {}; laneNames.forEach((l,i)=>laneIdx[l]=i);
const laneCount = {}; laneNames.forEach(l=>laneCount[l]=0);
const idCount = ids.map(()=>0);
events.forEach(e=>{ laneCount[e._lane]++; idCount[e.ii]++;
  e._hay = (ids[e.ii].name + " " + e.msg + " " + e._lane).toLowerCase(); });

const gridCols = "var(--gut) " + "minmax(150px,1fr) ".repeat(L);
const head = document.getElementById("laneHead");
head.style.gridTemplateColumns = gridCols;
head.innerHTML = '<div>time / Δ</div>' + laneNames.map(n=>'<div>'+esc(n)+'</div>').join("");

/* ---------- 時刻フォーマット ---------- */
function fmtDur(ns){
  const a = Math.abs(ns);
  if(a < 1e3)  return ns + " ns";
  if(a < 1e6)  return trim(ns/1e3)  + " µs";
  if(a < 1e9)  return trim(ns/1e6)  + " ms";
  if(a < 60e9) return trim(ns/1e9)  + " s";
  if(a < 3600e9) return trim(ns/60e9) + " min";
  return trim(ns/3600e9) + " h";
}
function trim(v){ return (Math.round(v*1000)/1000).toLocaleString(); }
function fmtAbs(rel){
  if(D.epoch !== "unix"){
    const s = Math.floor(rel/1e9), frac = rel - s*1e9;
    return s + "." + String(frac).padStart(9,"0").replace(/(\d{3})(\d{3})(\d{3})/,"$1 $2 $3") + " s";
  }
  let nsec = D.t0Nsec + rel;
  const sec = D.t0Sec + Math.floor(nsec/1e9);
  nsec = nsec % 1e9;
  const d = new Date(sec*1000);
  const p = n=>String(n).padStart(2,"0");
  return p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds())
       + "." + String(nsec).padStart(9,"0").replace(/(\d{3})(\d{3})(\d{3})/,"$1 $2 $3");
}

/* ---------- ヘッダ・注意書き ---------- */
document.getElementById("hMeta").textContent =
  D.meta.file + "  |  " + D.meta.size_bytes.toLocaleString() + " bytes  |  record " +
  D.meta.record_size + " B × " + N.toLocaleString() + " logs  |  span " +
  (N ? fmtDur(events[N-1].t - events[0].t) : "-");
document.getElementById("foot").textContent =
  "generated " + D.meta.generated + " / format: " + D.meta.format_file;
const notices = document.getElementById("notices");
function notice(msg){ const d=document.createElement("div"); d.className="notice"; d.textContent=msg; notices.appendChild(d); }
if(D.meta.remainder) notice("ファイル末尾に端数 " + D.meta.remainder + " B があります(フォーマット不一致の可能性)。");
if(D.meta.order_warn) notice("タイムスタンプが昇順でないレコードがありました。時刻順に並べ替えて表示しています(カウンタ折り返しの場合は timestamp.wrap_bits の指定を検討してください)。");

/* ---------- レーンチップ ---------- */
const laneActive = laneNames.map(()=>true);
const chipHost = document.getElementById("laneChips");
laneNames.forEach((l,i)=>{
  const c = document.createElement("span");
  c.className = "chip";
  c.innerHTML = esc(l) + ' <span class="n">'+laneCount[l].toLocaleString()+'</span>';
  c.onclick = ()=>{ laneActive[i]=!laneActive[i]; c.classList.toggle("off",!laneActive[i]); layout(); };
  chipHost.appendChild(c);
});

/* ---------- ID 別件数 ---------- */
const qEl = document.getElementById("q");
const grid = document.getElementById("idGrid");
{
  const order = ids.map((t,i)=>({t,i})).filter(x=>idCount[x.i])
                   .sort((a,b)=>idCount[b.i]-idCount[a.i]);
  order.slice(0, ID_CHIP_CAP).forEach(({t,i})=>{
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = '<span class="lv" style="background:'+t.color+'"></span>'
                + esc(t.name)+' <span class="n">'+idCount[i].toLocaleString()+'</span>';
    c.title = "id=" + t.key + " / level=" + t.level;
    c.onclick = ()=>{ qEl.value = (qEl.value===t.name) ? "" : t.name; layout(); };
    grid.appendChild(c);
  });
  if(order.length > ID_CHIP_CAP){
    const m=document.createElement("span"); m.className="more";
    m.textContent = "…他 " + (order.length-ID_CHIP_CAP) + " 種 (検索欄で指定可)";
    grid.appendChild(m);
  }
}

/* ---------- レーン背景線 ---------- */
const body = document.getElementById("tlBody");
(function laneLines(){
  const imgs=[], pos=[];
  for(let i=0;i<L;i++){
    imgs.push("linear-gradient(var(--lane),var(--lane))");
    pos.push("calc(var(--gut) + (100% - var(--gut))*"+((i+0.5)/L).toFixed(4)+") 0");
  }
  body.style.backgroundImage=imgs.join(",");
  body.style.backgroundPosition=pos.join(",");
  body.style.backgroundSize="1px 100%";
  body.style.backgroundRepeat="no-repeat";
})();

/* ==================================================================
   仮想スクロール: フィルタ後の全アイテムの y 座標だけ先に計算し、
   画面内 (±BUF px) のアイテムのみ DOM 化する。数十万件まで対応。
   ================================================================== */
const modeEl=document.getElementById("mode"), breakEl=document.getElementById("breakF"),
      zoomEl=document.getElementById("zoom"), lvEl=document.getElementById("lvSel");
qEl.addEventListener("input", debounce(layout,200));
modeEl.onchange=layout; breakEl.onchange=layout; lvEl.onchange=layout;
zoomEl.addEventListener("input", debounce(layout,120));
function debounce(fn,ms){let t;return()=>{clearTimeout(t);t=setTimeout(fn,ms);};}

function median(arr){ if(!arr.length) return 0;
  const s=Float64Array.from(arr).sort(); const m=s.length>>1;
  return s.length%2 ? s[m] : (s[m-1]+s[m])/2; }

let items=[], winA=-1, winB=-2, selKey=null;

function layout(){
  const q = qEl.value.toLowerCase();
  const minLv = +lvEl.value;
  const vis = [];
  for(const e of events){
    if(!laneActive[laneIdx[e._lane]]) continue;
    if(LV[ids[e.ii].level] < minLv) continue;
    if(q && !e._hay.includes(q)) continue;
    vis.push(e);
  }
  document.getElementById("cnt").textContent =
    vis.length.toLocaleString() + " / " + N.toLocaleString() + " logs";

  const deltas=[];
  for(let i=1;i<vis.length;i++){ const d=vis[i].t-vis[i-1].t; if(d>0) deltas.push(d); }
  const tau = median(deltas) || 1;
  const bf = +breakEl.value;
  const zoom = +zoomEl.value;
  const uniform = modeEl.value === "uniform";
  const GAP_MIN = 2, GAP_MAX = 130;

  items = [];
  let y = 6, prevT = null;
  for(const e of vis){
    const dt = prevT===null ? 0 : e.t - prevT;
    if(prevT!==null){
      const doBreak = bf>0 && dt > bf*tau && dt > 0;
      if(doBreak){
        items.push({br:dt, y}); y += BREAK_H + 4;
      }else if(uniform){
        y += 6;
      }else if(dt>0){
        y += Math.min(GAP_MAX, GAP_MIN + Math.log10(1 + dt/tau) * zoom);
      }else{
        y += GAP_MIN;
      }
    }
    items.push({e, y, dt, first: prevT===null});
    y += ROW_H;
    prevT = e.t;
  }
  body.style.height = (y + 30) + "px";
  winA=-1; winB=-2;
  renderWindow(true);
}

function findIdx(yy){
  let lo=0, hi=items.length;
  while(lo<hi){ const m=(lo+hi)>>1; if(items[m].y < yy) lo=m+1; else hi=m; }
  return Math.max(0, lo-1);
}

function renderWindow(force){
  if(!items.length){ body.replaceChildren(); winA=-1; winB=-2; return; }
  const top = body.getBoundingClientRect().top;
  const vh = window.innerHeight || 800;
  const lo = -top - BUF, hi = -top + vh + BUF;
  let a = findIdx(lo);
  let b = a;
  while(b < items.length-1 && items[b+1].y <= hi) b++;
  if(!force && a===winA && b===winB) return;
  winA=a; winB=b;
  const frag = document.createDocumentFragment();
  for(let i=a;i<=b;i++) frag.appendChild(makeNode(items[i]));
  body.replaceChildren(frag);
}

function makeNode(it){
  if(it.br !== undefined){
    const br = document.createElement("div");
    br.className = "break";
    br.style.top = it.y + "px";
    br.innerHTML = '<span class="line"></span><span class="lbl">≈ '
                 + esc(fmtDur(it.br)) + ' 経過</span><span class="line"></span>';
    return br;
  }
  const e = it.e, id = ids[e.ii];
  const row = document.createElement("div");
  row.className = "row";
  row.style.gridTemplateColumns = gridCols;
  row.style.top = it.y + "px";

  const gut = document.createElement("div");
  gut.className = "gut";
  gut.innerHTML = '<span class="abs">' + esc(fmtAbs(e.t)) + '</span>'
    + '<span class="dlt">' + (it.first ? "#"+e.n : "+" + esc(fmtDur(it.dt))) + '</span>';
  row.appendChild(gut);

  const cell = document.createElement("div");
  cell.className = "cell";
  cell.style.gridColumn = (laneIdx[e._lane] + 2);
  const chip = document.createElement("span");
  chip.className = "evt lv-" + id.level + (selKey===e.n ? " sel" : "");
  chip.style.borderLeftColor = id.color;
  chip.innerHTML = '<span class="nm" style="color:'+id.color+'">'+esc(id.name)+'</span>'
                 + (e.msg ? '<span class="msg">'+esc(e.msg)+'</span>' : "");
  chip.onclick = ()=>showDetail(e, id, chip);
  cell.appendChild(chip);
  row.appendChild(cell);
  return row;
}

window.addEventListener("scroll", rafRender, {passive:true});
window.addEventListener("resize", rafRender);
const raf_ = window.requestAnimationFrame ? window.requestAnimationFrame.bind(window)
            : (f)=>setTimeout(f,16);
let raf=0;
function rafRender(){ if(raf) return;
  raf = raf_(()=>{ raf=0; renderWindow(false); }); }

/* ---------- 詳細パネル ---------- */
const dp = document.getElementById("dpanel");
document.getElementById("dpClose").onclick = ()=>{
  dp.style.display="none"; selKey=null; renderWindow(true); };
function showDetail(e, id, chip){
  selKey = e.n;
  document.querySelectorAll(".evt.sel").forEach(c=>c.classList.remove("sel"));
  chip.classList.add("sel");
  document.getElementById("dpTitle").textContent = "#" + e.n + "  " + id.name;
  let rows = '<tr><td>time</td><td>'+esc(fmtAbs(e.t))+' (T+'+esc(fmtDur(e.t))+')</td></tr>'
    + '<tr><td>id</td><td>'+esc(id.key)+' ('+esc(id.level)+')</td></tr>'
    + '<tr><td>lane</td><td>'+esc(e._lane)+'</td></tr>'
    + (e.msg ? '<tr><td>message</td><td>'+esc(e.msg)+'</td></tr>' : "");
  for(const k in e.d){
    const raw = e.d[k][0], disp = e.d[k][1];
    const extra = (String(raw)!==String(disp))
      ? '  <span style="color:#8b9aa8">(raw: '+esc(raw)+')</span>' : "";
    rows += '<tr><td>'+esc(k)+'</td><td>'+esc(disp)+extra+'</td></tr>';
  }
  document.getElementById("dpBody").innerHTML = '<table>'+rows+'</table>';
  dp.style.display = "block";
}

layout();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 入力チェック

def looks_like_text(data: bytes) -> bool:
    """入力がテキストファイル(CSV等)らしいかの簡易判定"""
    sample = data[:4096]
    if not sample:
        return False
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True  # BOM
    if b"\x00" in sample:
        return False  # NUL を含めばバイナリとみなす
    printable = sum(1 for b in sample
                    if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
    return printable / len(sample) > 0.90


def safe_out_path(out_path: Path, in_path: Path, tag: str) -> Path:
    """出力先が入力ファイル自身なら別名に退避する"""
    try:
        same = out_path.resolve() == in_path.resolve()
    except OSError:
        same = out_path == in_path
    if same:
        alt = out_path.with_name(out_path.stem + f"_{tag}" + out_path.suffix)
        print(f"  注意: 出力先が入力ファイルと同一のため {alt.name} に変更しました")
        return alt
    return out_path


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="trace_log(id, data) 形式トレースログビューワ")
    ap.add_argument("logfile")
    ap.add_argument("-f", "--format", required=True, help="フォーマット定義 JSON")
    ap.add_argument("-c", "--csv", help="CSV 出力先 (省略時 <logfile>.csv)")
    ap.add_argument("-o", "--html", help="HTML 出力先 (省略時 <logfile>.html)")
    ap.add_argument("--limit", type=int, help="先頭 N レコードのみ")
    ap.add_argument("--raw", action="store_true", help="CSV を生値で出力")
    ap.add_argument("--no-csv", action="store_true")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="入力の妥当性チェックを無視して続行")
    args = ap.parse_args(argv)

    log_path, fmt_path = Path(args.logfile), Path(args.format)
    try:
        spec = load_format(fmt_path)
        data = log_path.read_bytes()
    except (FormatError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if looks_like_text(data) and not args.force:
        print(f"エラー: {log_path.name} はテキストファイル(CSV 等)のようです。\n"
              f"  バイナリのログファイルを指定してください。"
              f"意図的にこのファイルを解析する場合は --force を付けてください。",
              file=sys.stderr)
        return 1

    try:
        entries, remainder, unknown, order_warn = decode_log(data, spec, args.limit)
    except (FormatError, struct.error) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    print(f"読込: {log_path.name} ({len(data):,} bytes) → {len(entries):,} ログ "
          f"(レコード長 {spec['_record_size']} B"
          + (f", 端数 {remainder} B" if remainder else "") + ")")
    n_unknown = sum(unknown.values())
    if unknown:
        top = sorted(unknown.items(), key=lambda kv: -kv[1])
        shown = ", ".join(f"{k}×{v}" for k, v in top[:8])
        more = f" 他{len(top) - 8}種" if len(top) > 8 else ""
        print(f"  注意: ID テーブルにない ID が {n_unknown} 件 "
              f"({len(top)} 種: {shown}{more})")
    if entries and n_unknown / len(entries) > 0.3:
        print("  警告: 未定義 ID が全体の "
              f"{100 * n_unknown / len(entries):.0f}% を占めます。"
              "レコード長・オフセット・エンディアンなどフォーマット定義が"
              "実データと合っていない可能性があります。")
    if order_warn:
        print("  注意: タイムスタンプ非昇順 → 時刻順に並べ替え "
              "(折り返しなら timestamp.wrap_bits を指定してください)")

    if not entries:
        print("ログがありません", file=sys.stderr)
        return 1

    if not args.no_csv:
        csv_path = Path(args.csv) if args.csv else log_path.with_suffix(".csv")
        csv_path = safe_out_path(csv_path, log_path, "out")
        write_csv(csv_path, spec, entries, raw=args.raw)
        print(f"CSV : {csv_path}")

    if not args.no_html:
        meta = {
            "file": log_path.name, "format_file": fmt_path.name,
            "size_bytes": len(data), "record_size": spec["_record_size"],
            "remainder": remainder, "order_warn": order_warn,
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        html_path = Path(args.html) if args.html else log_path.with_suffix(".html")
        html_path = safe_out_path(html_path, log_path, "out")
        html_path.write_text(build_html(spec, entries, meta), encoding="utf-8")
        print(f"HTML: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
