#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
体育判罚累计与申诉级联重算工具（纯 Python 标准库，单文件）

用法:
    python3 referee.py 输入.json          # 从文件读取
    cat 输入.json | python3 referee.py    # 从标准输入读取
    python3 referee.py --demo             # 运行内置示例

==================== 自定规则说明 ====================

【证据不足的处理】（证据等级: 无=0 < 证人=1 < 视频=2）
  1. 证据达到规则要求的最低等级 -> 按规则原判罚执行。
  2. 证据低于要求、但并非完全没有证据 -> 每差一级，判罚降一档
     （红牌->黄牌->罚分，最低降到罚分为止）。
     理由: 有部分证据说明犯规大概率存在，但证明力不足，
           降档既不放纵犯规，也避免证据不足时的过重处罚。
  3. 证据为"无"而规则要求证据 -> 判罚记为"待裁决"，暂不计入
     累计，等待补充证据。
     理由: 完全无证据时任何实质判罚都缺乏依据，挂起等待
           比直接处罚或直接放过都更公平。

【跨场次累计升级】
  同一运动员在所有场次中按事件顺序累计黄牌，累计到第 2 张时，
  该第 2 张黄牌升级为红牌（"两黄变一红"），随后黄牌累计清零
  重新计数。红牌与罚分直接累计，不再升级。
  理由: 借鉴足球两黄一红规则并推广到跨场次，防止运动员
        分散在多场次的轻微犯规逃避重罚。

【申诉与级联重算】
  申诉结果为"改判"时，对应事件的判罚被撤销（视为未发生），
  然后整个事件流按顺序重新计算：该事件之后的黄牌累计、
  两黄变一红升级、罚分合计都会级联更新。输出中给出
  申诉前/申诉后两套结果及其差异，便于核对级联影响。
"""

import json
import sys
from collections import defaultdict

EVIDENCE_LEVEL = {"无": 0, "证人": 1, "视频": 2}
PENALTY_LEVEL = {"罚分": 1, "黄牌": 2, "红牌": 3}
LEVEL_PENALTY = {v: k for k, v in PENALTY_LEVEL.items()}
VALID_APPEAL_RESULTS = {"维持", "改判"}


def compute(rules, athletes, matches, events, appeals):
    """核心计算。返回 (逐事件判罚记录, 运动员累计, 错误清单)。"""
    errors = []
    records = []

    rule_map = {r.get("犯规类型"): r for r in rules}
    athlete_set = set(athletes)
    match_map = {m.get("名称"): set(m.get("运动员列表", [])) for m in matches}

    # ---- 处理申诉：收集被改判撤销的事件下标 ----
    invalidated = set()
    for i, ap in enumerate(appeals):
        idx = ap.get("事件")
        result = ap.get("结果")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(events)):
            errors.append("申诉#%d: 引用不存在的事件编号 %r" % (i, idx))
            continue
        if result not in VALID_APPEAL_RESULTS:
            errors.append("申诉#%d: 无效的申诉结果 %r（应为 维持/改判）" % (i, result))
            continue
        if result == "改判":
            invalidated.add(idx)

    # ---- 顺序处理事件流 ----
    yellow_count = defaultdict(int)          # 用于两黄变一红的滚动计数
    totals = {a: {"黄牌": 0, "红牌": 0, "罚分": 0} for a in athletes}
    seen_events = set()

    for idx, ev in enumerate(events):
        rec = {"事件编号": idx, "场次": ev.get("场次"), "运动员": ev.get("运动员"),
               "犯规类型": ev.get("犯规类型"), "证据": ev.get("证据"),
               "判罚": None, "备注": ""}

        # 申诉改判 -> 撤销，不再参与任何累计（级联重算的体现）
        if idx in invalidated:
            rec["判罚"] = "已撤销"
            rec["备注"] = "申诉改判，判罚撤销，不参与累计"
            records.append(rec)
            continue

        # 引用完整性检查
        match_name = ev.get("场次")
        athlete = ev.get("运动员")
        foul = ev.get("犯规类型")
        evidence = ev.get("证据")
        bad = False
        if match_name not in match_map:
            errors.append("事件#%d: 引用不存在的场次 %r" % (idx, match_name))
            bad = True
        if athlete not in athlete_set:
            errors.append("事件#%d: 引用不存在的运动员 %r" % (idx, athlete))
            bad = True
        elif match_name in match_map and athlete not in match_map[match_name]:
            errors.append("事件#%d: 运动员 %r 未登记在场次 %r 的运动员列表中"
                          % (idx, athlete, match_name))
            bad = True
        if foul not in rule_map:
            errors.append("事件#%d: 犯规类型 %r 无对应规则" % (idx, foul))
            bad = True
        if evidence not in EVIDENCE_LEVEL:
            errors.append("事件#%d: 无效证据类型 %r（应为 视频/证人/无）" % (idx, evidence))
            bad = True
        if bad:
            rec["判罚"] = "无效"
            rec["备注"] = "事件存在错误，未判罚"
            records.append(rec)
            continue

        # 同一事件重复判罚检查（同场次+同运动员+同犯规类型视为同一事件）
        key = (match_name, athlete, foul)
        if key in seen_events:
            errors.append("事件#%d: 重复判罚——与之前同场次(%s)、同运动员(%s)、"
                          "同犯规类型(%s)的事件重复，已忽略" % (idx, match_name, athlete, foul))
            rec["判罚"] = "重复"
            rec["备注"] = "重复事件，已忽略"
            records.append(rec)
            continue
        seen_events.add(key)

        # ---- 按规则判罚 + 证据不足降级/等待 ----
        rule = rule_map[foul]
        base = rule.get("判罚")
        if base not in PENALTY_LEVEL:
            errors.append("事件#%d: 规则 %r 的判罚 %r 无效（应为 黄牌/红牌/罚分）"
                          % (idx, foul, base))
            rec["判罚"] = "无效"
            rec["备注"] = "规则定义错误"
            records.append(rec)
            continue

        required = EVIDENCE_LEVEL.get(rule.get("证据要求"), 0)
        actual = EVIDENCE_LEVEL[evidence]
        if actual >= required:
            final = base
        elif actual == 0:
            final = "待裁决"
            rec["备注"] = "无证据，判罚挂起等待补充证据，暂不计入累计"
        else:
            gap = required - actual
            final = LEVEL_PENALTY[max(PENALTY_LEVEL[base] - gap, 1)]
            rec["备注"] = "证据不足（要求%s，实际%s），%s降级为%s" % (
                rule.get("证据要求"), evidence, base, final)

        # ---- 跨场次累计：两黄变一红 ----
        if final == "黄牌":
            yellow_count[athlete] += 1
            if yellow_count[athlete] == 2:
                final = "红牌"
                yellow_count[athlete] = 0
                upgrade_note = "两黄变一红（跨场次累计第2张黄牌升级为红牌）"
                rec["备注"] = (rec["备注"] + "；" + upgrade_note) if rec["备注"] else upgrade_note

        if final in totals[athlete]:
            totals[athlete][final] += 1

        rec["判罚"] = final
        records.append(rec)

    return records, totals, errors


def diff_results(before_records, before_totals, after_records, after_totals):
    """比较申诉前后的结果，列出级联影响。"""
    changes = []
    for b, a in zip(before_records, after_records):
        if b["判罚"] != a["判罚"]:
            changes.append("事件#%d（%s/%s/%s）: %s -> %s" % (
                b["事件编号"], b["场次"], b["运动员"], b["犯规类型"], b["判罚"], a["判罚"]))
    for athlete in sorted(set(before_totals) | set(after_totals)):
        b, a = before_totals.get(athlete), after_totals.get(athlete)
        if b != a:
            changes.append("运动员 %s 累计: %s -> %s" % (athlete, b, a))
    return changes


def run(data):
    rules = data.get("规则", [])
    athletes = [a.get("名称") for a in data.get("运动员", [])]
    matches = data.get("场次", [])
    events = data.get("事件流", [])
    appeals = data.get("申诉流", [])

    # 申诉前结果
    pre_records, pre_totals, _ = compute(rules, athletes, matches, events, [])
    # 申诉后结果（级联重算）
    records, totals, errors = compute(rules, athletes, matches, events, appeals)

    output = {
        "判罚结果": records,
        "运动员累计": totals,
        "申诉级联影响": diff_results(pre_records, pre_totals, records, totals) if appeals else [],
        "错误报告": errors,
    }
    return output


DEMO_INPUT = {
    "规则": [
        {"犯规类型": "恶意犯规", "判罚": "红牌", "证据要求": "视频"},
        {"犯规类型": "技术犯规", "判罚": "黄牌", "证据要求": "证人"},
        {"犯规类型": "延误比赛", "判罚": "罚分", "证据要求": "证人"}
    ],
    "运动员": [{"名称": "张三"}, {"名称": "李四"}, {"名称": "王五"}],
    "场次": [
        {"名称": "初赛", "运动员列表": ["张三", "李四", "王五"]},
        {"名称": "决赛", "运动员列表": ["张三", "李四"]}
    ],
    "事件流": [
        {"场次": "初赛", "运动员": "张三", "犯规类型": "技术犯规", "证据": "证人"},
        {"场次": "初赛", "运动员": "李四", "犯规类型": "恶意犯规", "证据": "证人"},
        {"场次": "初赛", "运动员": "王五", "犯规类型": "恶意犯规", "证据": "无"},
        {"场次": "决赛", "运动员": "张三", "犯规类型": "技术犯规", "证据": "视频"},
        {"场次": "决赛", "运动员": "张三", "犯规类型": "技术犯规", "证据": "证人"},
        {"场次": "决赛", "运动员": "赵六", "犯规类型": "技术犯规", "证据": "证人"},
        {"场次": "决赛", "运动员": "李四", "犯规类型": "咬人", "证据": "视频"},
        {"场次": "加时赛", "运动员": "张三", "犯规类型": "延误比赛", "证据": "证人"}
    ],
    "申诉流": [
        {"事件": 0, "结果": "改判"},
        {"事件": 3, "结果": "维持"},
        {"事件": 99, "结果": "改判"}
    ]
}


def main(argv):
    if "--demo" in argv:
        data = DEMO_INPUT
    elif len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    json.dump(run(data), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv)
