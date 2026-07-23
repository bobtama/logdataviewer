#!/usr/bin/env python3
"""sample_trace_format.json 対応のデモトレースログを生成する。
trace_log(id, data) 相当。µs タイムスタンプ(uint32, 折り返しあり)で、
制御周期の細かいログと数秒〜数十秒のアイドルが混在する。"""
import random
import struct

random.seed(11)
REC = 12
out = []
# uint32 µs カウンタが途中で折り返すよう、末尾近くから開始
t = (1 << 32) - 40_000_000  # 折り返し 40 秒前


def log(t_us, id_, task, data=0):
    out.append(struct.pack("<IHBxI", t_us & 0xFFFFFFFF, id_, task, data & 0xFFFFFFFF))


log(t, 1, 1, 0x00010203)          # SYS_START
t += 1500
log(t, 2, 1, 1)                   # STATE → IDLE
t += random.randint(800_000, 1_200_000)

seq = 0
temp = 6500
for phase in range(8):
    log(t, 2, 1, 2)               # STATE → RUN
    t += random.randint(200, 900)
    for _ in range(random.randint(6, 15)):     # 制御周期ごとの細かいログ
        seq += 1
        log(t, 10, 1, seq)                     # CTRL_CYCLE
        t += random.randint(30, 120)           # 数十 µs
        temp += random.randint(-15, 18)
        log(t, 12, 1, temp)                    # ADC_TEMP
        t += random.randint(20, 80)
        log(t, 11, 1, random.randint(-800, 800) & 0xFFFFFFFF)  # MOTOR_SET
        t += random.randint(50, 300)
        if random.random() < 0.6:
            cid = random.choice([0x101, 0x1A0, 0x2F5])
            log(t, 20, 2, cid)                 # CAN_TX
            t += random.randint(120, 900)
            if random.random() < 0.85:
                log(t, 21, 0, cid)             # CAN_RX (ISR)
            else:
                t += random.randint(4_000, 9_000)
                log(t, 22, 2, cid)             # CAN_TIMEOUT
                if random.random() < 0.5:
                    t += random.randint(200, 800)
                    log(t, 90, 2, 257)         # ERR E_TIMEOUT
            t += random.randint(100, 500)
        usage = random.randint(20, 96)
        if random.random() < 0.25:
            log(t, 31 if usage > 85 else 30, 3, usage)  # BUF
            t += random.randint(30, 150)
        t += random.randint(800, 2_500)        # 周期間 ~1-2.5ms
    if random.random() < 0.15:
        log(t, 90, 1, random.choice([258, 515, 999]))
        t += random.randint(100, 500)
    log(t, 2, 1, 1)               # STATE → IDLE
    t += random.randint(1, 25) * 1_000_000     # 1〜25 s アイドル

with open("sample_trace.log", "wb") as fp:
    fp.write(b"".join(out))
print(f"sample_trace.log written: {len(out)} records x {REC} bytes "
      f"(uint32 µs counter wraps mid-log)")
