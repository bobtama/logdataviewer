# tracelog_viewer — trace_log(id, data) トレースログビューワ

タスク等から `trace_log(id, data);` で記録された固定長バイナリログのビューワです。
ID テーブル(JSON)でメッセージ文字列と data の解釈を定義し、CSV と
単一ファイルの HTML タイムラインを出力します。Python 3.8+ 標準ライブラリのみ。

## 使い方

```bash
python3 tracelog_viewer.py <logfile> -f format.json [-c out.csv] [-o out.html]
        [--raw] [--limit N] [--no-csv] [--no-html]
```

デモ:

```bash
python3 make_sample_tracelog.py
python3 tracelog_viewer.py sample_trace.log -f sample_trace_format.json
```

## HTML タイムライン

- 上→下が時系列。左端に時刻(相対 `T+x.xxx s` または実時刻、ns 精度)と直前からの Δ
- **レーン(列)はタスク別**(`lane.field` でレコード内のフィールドを指定)。
  ID 側の `lane` 指定で上書きも可能。指定なしなら1列
- 各ログは `ID名 + メッセージ` のチップ表示。`text` テンプレートの `{data}` に
  解釈済みの data(スケール・単位・enum 置換後)が入る。他フィールドも `{task}` 等で参照可
- **縦の間隔は発生間隔を log スケールで反映**(中央値 τ 基準)。
  µs 間隔の制御ログと秒単位のアイドルが混在しても潰れない
- **桁違いの間隔(既定 ×1000τ 超)は省略線で圧縮**し「≈ 25 s 経過」と表示。
  しきい値切替・等間隔表示・倍率スライダーあり
- `level`(debug/info/warn/error)で色分け。レベルフィルタ(warn 以上のみ等)、
  タスクチップでのレーンフィルタ、ID名・メッセージの全文検索
- 「ID 別件数」パネルでどの ID が何回出たか一覧、クリックで絞り込み
- ログクリックで詳細(生値含む)を展開

## フォーマット定義 (JSON)

```jsonc
{
  "name": "ログ名",
  "endian": "little",
  "record_size": 12,
  "header_bytes": 0,

  "timestamp": {
    "field": "timestamp",
    "unit": "us",              // ns/us/ms/s または "scale_ns": tick→ns 係数
    "epoch": "relative",       // "relative"=T+表示 / "unix"=実時刻表示
    "wrap_bits": 32            // 任意: カウンタ折り返しを自動 unwrap
  },
  "id":         { "field": "id" },
  "data_field": "data",        // data ワードのフィールド名
  "lane":       { "field": "task" },   // 任意: 列分けに使うフィールド

  "fields": [                  // レコードレイアウト
    { "name": "timestamp", "type": "uint32" },
    { "name": "id",        "type": "uint16" },
    { "name": "task",      "type": "uint8",
      "map": { "0": "ISR", "1": "CtrlTask" } },
    { "type": "pad", "length": 1 },
    { "name": "data",      "type": "uint32" }
  ],

  "ids": {                     // trace_log の ID テーブル
    "11": {
      "name": "MOTOR_SET",
      "text": "モータ出力 {data}",         // メッセージ ({data} に解釈後の値)
      "data": {                             // この ID での data の解釈
        "type": "int32",                    //   ビット再解釈 (int32/float32等)
        "scale": 0.1, "unit": "%"           //   スケール・単位
      },
      "level": "info",                      // debug/info/warn/error (色と絞り込み)
      "lane": "モータ",                     // 任意: レーン上書き
      "color": "#1a8a4a"                    // 任意: 色上書き
    },
    "90": {
      "name": "ERR", "text": "エラー {data}", "level": "error",
      "data": { "map": { "257": "E_TIMEOUT" }, "map_default": "E_UNKNOWN({value})" }
    },
    "default": { "name": "TRACE", "text": "data={data}", "level": "debug" }
  }
}
```

`data` の解釈には `type`(同じ 4/8 バイトを int32/float32 等でビット再解釈)、
`scale`/`add`、`unit`、`display`(hex/bin)、`map`/`map_default`(enum 置換)が使えます。
未定義 ID は `default` エントリで処理(未指定なら `ID_0x..` として自動表示し、
コンソールに件数を警告)。

## CSV 出力

`record_no, time_ns, delta_ns, id, name, level, lane, message, data, <その他フィールド>`。
message は HTML と同じ整形済み文字列なので、そのまま grep や Excel フィルタで
分析できます。`--raw` で data・各フィールドを置換前の生値で出力します。
