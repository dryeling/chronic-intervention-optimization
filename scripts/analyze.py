import argparse
import hashlib
import math
import platform
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_COLUMNS = [
    "intervention_id", "created_at", "device_synced_at", "center_id",
    "plan_type", "buffer_days", "msg_count", "result_ok",
]
NUMERIC_COLUMNS = ["buffer_days", "msg_count", "result_ok"]
TIME_COLUMNS = ["created_at", "device_synced_at"]


def format_value(value):
    if pd.isna(value):
        return "NA"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(format_value(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def load_data(source):
    raw = pd.read_csv(source, dtype="string", keep_default_na=False)
    if list(raw.columns) != EXPECTED_COLUMNS:
        raise ValueError(f"Unexpected columns: {list(raw.columns)}")
    data = raw.apply(lambda column: column.str.strip()).replace("", pd.NA)
    parsed = data.copy()
    for column in NUMERIC_COLUMNS:
        parsed[column] = pd.to_numeric(data[column], errors="coerce")
    for column in TIME_COLUMNS:
        parsed[column] = pd.to_datetime(data[column], utc=True, errors="coerce", format="ISO8601")
    parsed["data_lag_hours"] = (
        parsed["created_at"] - parsed["device_synced_at"]
    ).dt.total_seconds() / 3600
    return raw, data, parsed


def outcome_counts(data):
    outcome = pd.to_numeric(data["result_ok"], errors="coerce")
    success = int(outcome.eq(1).sum())
    failed = int(outcome.eq(0).sum())
    missing = int(data["result_ok"].isna().sum())
    invalid = len(data) - success - failed - missing
    returned = success + failed
    assert success + failed + missing + invalid == len(data)
    return success, failed, missing, invalid, returned


def audit(source, output):
    raw, data, parsed = load_data(source)
    success, failed, missing, invalid, returned = outcome_counts(data)
    total = len(data)
    failure_rate = f"{failed / returned:.2%}" if returned else "NA (no valid outcomes)"
    missing_rate = f"{missing / total:.2%}" if total else "NA"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    lines = [
        "# 数据审计：慢病干预失效分析",
        "",
        "## 复现与口径",
        "",
        "- 唯一数据源：`interventions.csv`；只读，不补造、不删除原始记录。",
        "- 运行：`python -m pip install pandas`，然后 `python scripts/analyze.py`。",
        f"- 实际输入路径：`{source.as_posix()}`。",
        f"- 环境：Python {platform.python_version()}，pandas {pd.__version__}。",
        f"- 输入文件 SHA-256：`{source_hash}`。",
        "- 空字符串及仅空白的单元格视为缺失；保留文本 NA/null 等为原始值，单独识别非法数值。",
        "- `result_ok` 空值为未回传，绝不填为 0；失效率仅为 0 / (0 + 1)。",
        "- 两个时间字段按其显式时区解析并统一为 UTC；报告时间范围采用 UTC。",
        "- `data_lag_hours = (created_at - device_synced_at).total_seconds() / 3600`。",
        "",
        "## 1. 数据结构与缺失",
        "",
        f"实际行数：{total}；原始列数：{len(data.columns)}；派生字段不计入原始列数。",
        "",
    ]
    schema_rows = []
    for column in data.columns:
        series = parsed[column]
        semantic = "业务编号（字符串）" if column == "intervention_id" else "类别（字符串）"
        if column in TIME_COLUMNS:
            semantic = "带时区时间（UTC）"
        elif column in NUMERIC_COLUMNS:
            semantic = "数值（非空值均为整数）" if series.dropna().mod(1).eq(0).all() else "数值"
        parse_errors = int((data[column].notna() & series.isna()).sum())
        schema_rows.append((column, str(series.dtype), semantic, int(data[column].isna().sum()),
                            parse_errors, int(data[column].nunique())))
    lines += [table(["列名", "解析后类型", "语义类型", "原始缺失数", "非空解析失败数", "非空唯一值数"], schema_rows), ""]
    duplicate_ids = data["intervention_id"].notna() & data["intervention_id"].duplicated(keep=False)
    lines += [f"完全重复行数（不含首次）：{int(raw.duplicated().sum())}；"
              f"重复业务编号涉及行数（包含首次）：{int(duplicate_ids.sum())}。", "",
              "本次所有统计采用原始行口径，不自动去重。重复记录可能造成重复加权；后续根因分析应核实业务编号唯一性，并对去重口径做敏感性比较。", "",
              "## 2. 干预结果分布", "",
              table(["结果", "行数"], [("1：遵从", success), ("0：失效", failed),
                    ("空：未回传", missing), ("非法非空结果（非 0/1）", invalid)]), "",
              f"有效回传数（分母） = {failed} + {success} = {returned}。", "",
              f"失效率 = {failed} / {returned} = {failure_rate}；未回传率 = {missing_rate}。", "",
              "未回传及非法结果均不进入失效率分母；未回传机制未知，已回传样本可能存在选择偏差。", "",
              "## 3. 时间范围与数据延迟", ""]
    lines += [table(["字段", "有效时间数", "最早（UTC）", "最晚（UTC）"],
                    [(column, int(parsed[column].notna().sum()), parsed[column].min(), parsed[column].max())
                     for column in TIME_COLUMNS]), ""]
    lag = parsed["data_lag_hours"]
    negative = lag.lt(0)
    lines += [table(["延迟状态", "行数"], [("负值（异常标记，保留）", int(negative.sum())),
              ("等于 0", int(lag.eq(0).sum())), ("大于 0", int(lag.gt(0).sum())),
              ("缺失或无法计算", int(lag.isna().sum()))]), "",
              "负延迟可能与时间戳异常有关；不能据此确认原因，也不能直接删除。", ""]
    if negative.any():
        examples = parsed.loc[negative, ["intervention_id", *TIME_COLUMNS, "data_lag_hours"]].head(20)
        lines += ["负延迟记录示例（最多 20 条）：", "", table(list(examples.columns), examples.itertuples(index=False, name=None)), ""]
    lines += ["## 4. 数值描述统计", "",
              "标准差为样本标准差（ddof=1）；分位数使用线性插值。空值不参与数值统计。",
              "`result_ok` 的均值等只描述可解析数值，正式结果口径以第 2 节为准。", ""]
    stats_columns = ["count", "mean", "std", "min", "25%", "50%", "75%", "max"]
    stats_rows = []
    for column in [*NUMERIC_COLUMNS, "data_lag_hours"]:
        summary = parsed[column].describe(percentiles=[0.25, 0.5, 0.75])
        stats_rows.append((column, *(summary.get(key, float("nan")) for key in stats_columns)))
    lines += [table(["字段", *stats_columns], stats_rows), "",
              "## 5. 各字段分布", "",
              "每个字段展示频数最高的 20 个取值；占比的分母为全部行数，包含缺失。完整频数见同目录 `01_distributions.csv`。",
              "原始字段按去除首尾空白后的文本取值统计；派生延迟按计算后的数值统计。", ""]
    distribution_rows = []
    for column in [*data.columns, "data_lag_hours"]:
        series = parsed[column] if column == "data_lag_hours" else data[column]
        counts = series.value_counts(dropna=False)
        entries = []
        for value, count in counts.items():
            is_missing = bool(pd.isna(value))
            label = "（缺失）" if is_missing else str(value)
            share = int(count) / total if total else 0
            entries.append((label, int(count), f"{share:.2%}"))
            distribution_rows.append((column, "" if is_missing else str(value), is_missing, int(count), share))
        assert int(counts.sum()) == total
        lines += [f"### {column}", "", table(["取值", "行数", "占比"], entries[:20]), ""]
        if len(entries) > 20:
            lines += [f"其余 {len(entries) - 20} 个取值合计 {sum(entry[1] for entry in entries[20:])} 行。", ""]
    lines += ["### 时间按日分布（UTC）", ""]
    for column in TIME_COLUMNS:
        daily = parsed[column].dt.strftime("%Y-%m-%d").value_counts().sort_index()
        lines += [f"#### {column}", "", table(["日期（UTC）", "行数"], daily.items()), ""]
    lines += ["## 6. 数据质量标记与分析边界", "",
              table(["检查项", "涉及行数"], [
                  ("buffer_days 为负", int(parsed["buffer_days"].lt(0).sum())),
                  ("msg_count 为负", int(parsed["msg_count"].lt(0).sum())),
                  ("buffer_days 非整数", int(parsed["buffer_days"].dropna().mod(1).ne(0).sum())),
                  ("msg_count 非整数", int(parsed["msg_count"].dropna().mod(1).ne(0).sum())),
              ]), "",
              "本报告只做审计，不推断根因、不建立规则、不做因果归因。所有原始记录保留。",
              "后续规则输入不得包含 result_ok；样本量小于 30 的组合必须标记为样本不足，不能下结论。", ""]
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(distribution_rows, columns=["field", "value", "is_missing", "count", "share"]).to_csv(
        output.with_name("01_distributions.csv"), index=False, encoding="utf-8-sig"
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    print(f"rows={total}, columns={len(data.columns)}, success={success}, failed={failed}, missing={missing}, invalid={invalid}")
    print(f"failure_rate={failure_rate}, denominator={returned}, negative_lag={int(negative.sum())}")
    print(f"Report: {output}")


BUCKET_LEVELS = {
    "lag_bucket": ["<1h", "1-6h", "6-24h", "1-3d", ">3d", "negative", "missing"],
    "buffer_bucket": ["<=1", "2-3", "4-7", ">7", "invalid", "missing"],
    "msg_bucket": ["0", "1-2", "3-5", ">5", "invalid", "missing"],
}


def bucket_features(features):
    lag = features["data_lag_hours"]
    buffer = features["buffer_days"]
    messages = features["msg_count"]
    buckets = pd.DataFrame(index=features.index)
    lag_labels = pd.Series("missing", index=features.index, dtype="string")
    for condition, label in [
        (lag.lt(0), "negative"), (lag.ge(0) & lag.lt(1), "<1h"),
        (lag.ge(1) & lag.lt(6), "1-6h"), (lag.ge(6) & lag.lt(24), "6-24h"),
        (lag.ge(24) & lag.le(72), "1-3d"), (lag.gt(72), ">3d"),
    ]:
        lag_labels.loc[condition.fillna(False)] = label
    buffer_labels = pd.Series("missing", index=features.index, dtype="string")
    for condition, label in [
        (buffer.lt(0), "invalid"), (buffer.ge(0) & buffer.le(1), "<=1"),
        (buffer.gt(1) & buffer.le(3), "2-3"), (buffer.gt(3) & buffer.le(7), "4-7"),
        (buffer.gt(7), ">7"),
    ]:
        buffer_labels.loc[condition.fillna(False)] = label
    message_labels = pd.Series("missing", index=features.index, dtype="string")
    for condition, label in [
        (messages.lt(0), "invalid"), (messages.eq(0), "0"),
        (messages.gt(0) & messages.le(2), "1-2"),
        (messages.gt(2) & messages.le(5), "3-5"), (messages.gt(5), ">5"),
    ]:
        message_labels.loc[condition.fillna(False)] = label
    for column, values in zip(BUCKET_LEVELS, [lag_labels, buffer_labels, message_labels]):
        buckets[column] = pd.Categorical(values, categories=BUCKET_LEVELS[column], ordered=True)
    return buckets


def wilson_interval(failed, returned):
    if not returned:
        return math.nan, math.nan
    proportion = failed / returned
    z_score = 1.959963984540054
    denominator = 1 + z_score ** 2 / returned
    midpoint = (proportion + z_score ** 2 / (2 * returned)) / denominator
    margin = z_score * math.sqrt(proportion * (1 - proportion) / returned + z_score ** 2 / (4 * returned ** 2)) / denominator
    return max(0, midpoint - margin), min(1, midpoint + margin)


def summarize_groups(buckets, outcome, keys):
    frame = buckets.assign(
        failed=outcome.eq(0).fillna(False).astype(int),
        success=outcome.eq(1).fillna(False).astype(int),
        missing=outcome.isna().astype(int),
        invalid=(outcome.notna() & ~outcome.isin([0, 1])).astype(int),
    )
    grouped = frame.groupby(keys, observed=False, dropna=False)
    summary = grouped.agg(total=("failed", "size"), failed=("failed", "sum"),
                          success=("success", "sum"), missing=("missing", "sum"),
                          invalid=("invalid", "sum")).reset_index()
    summary["returned"] = summary["failed"] + summary["success"]
    summary["failure_rate"] = summary["failed"] / summary["returned"].replace(0, float("nan"))
    intervals = [wilson_interval(failed, returned) for failed, returned in zip(summary["failed"], summary["returned"])]
    summary["ci_low"] = [interval[0] for interval in intervals]
    summary["ci_high"] = [interval[1] for interval in intervals]
    summary["eligible"] = summary["returned"].ge(30)
    assert summary["total"].sum() == len(frame)
    assert summary["returned"].sum() == int(outcome.isin([0, 1]).sum())
    assert (summary["total"] == summary[["failed", "success", "missing", "invalid"]].sum(axis=1)).all()
    return summary


def ranked_combinations(combinations):
    return combinations.loc[combinations["eligible"]].sort_values(
        ["failure_rate", "returned", *BUCKET_LEVELS],
        ascending=[False, False, True, True, True],
    )


def group_table(summary, keys):
    rows = []
    for _, row in summary.iterrows():
        rate = "NA" if pd.isna(row["failure_rate"]) else f"{row['failure_rate']:.2%}"
        interval = "NA" if not row["returned"] else f"{row['ci_low']:.2%} - {row['ci_high']:.2%}"
        status = "可作探索性比较" if row["eligible"] else "样本不足，不能下结论"
        rows.append((*(row[key] for key in keys), row["total"], row["returned"], row["failed"],
                     row["missing"], row["invalid"], rate, interval, status))
    return table([*keys, "全部行数", "有效回传数", "失效数", "未回传数", "非法结果数", "失效率", "95% Wilson 区间", "说明"], rows)


def plot_rootcause(singles, ranked, baseline, figures):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    figures.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    for axis, (key, summary) in zip(axes, singles.items()):
        summary = summary.loc[~summary[key].isin(["invalid", "missing"]) | summary["total"].gt(0)]
        positions = list(range(len(summary)))
        for position, (_, row) in zip(positions, summary.iterrows()):
            if row["returned"]:
                color = "#267b74" if row["eligible"] else "#999999"
                axis.bar(position, row["failure_rate"], color=color, width=0.65)
                axis.vlines(position, row["ci_low"], row["ci_high"], color="#222222")
                axis.text(position, min(0.95, row["ci_high"] + 0.03), f"n={row['returned']}\n{row['failure_rate']:.1%}", ha="center", fontsize=8)
            else:
                axis.text(position, 0.04, "n=0\nNA", ha="center", fontsize=8)
        axis.set_xticks(positions, summary[key].astype(str), rotation=30)
        axis.set_xlim(-0.6, len(summary) - 0.4)
        axis.set_title(key)
        axis.set_ylim(0, 1)
        axis.yaxis.set_major_formatter(PercentFormatter(1))
        axis.axhline(baseline, color="#ba4a38", linestyle="--", label=f"Overall {baseline:.2%}")
        axis.legend(fontsize=8)
        axis.set_ylabel("Failure rate (valid returned outcomes only)")
    figure.suptitle("Single-field buckets | 95% Wilson intervals | grey: n < 30")
    figure.savefig(figures / "02_bucket_failure_rates.png", dpi=170)
    plt.close(figure)
    figure, axis = plt.subplots(figsize=(12, max(4, min(10, len(ranked)) * 0.55)), constrained_layout=True)
    top = ranked.head(10).iloc[::-1]
    if top.empty:
        axis.text(0.5, 0.5, "No combinations with n >= 30", ha="center")
    else:
        labels = [f"lag={row['lag_bucket']}, buffer={row['buffer_bucket']}, msg={row['msg_bucket']}" for _, row in top.iterrows()]
        positions = list(range(len(top)))
        axis.barh(positions, top["failure_rate"], color="#267b74")
        for position, (_, row) in zip(positions, top.iterrows()):
            axis.hlines(position, row["ci_low"], row["ci_high"], color="#222222")
            axis.text(1.01, position, f"{row['failure_rate']:.2%} ({row['failed']}/{row['returned']})", va="center", fontsize=9)
        axis.set_yticks(positions, labels)
    axis.axvline(baseline, color="#ba4a38", linestyle="--", label=f"Overall {baseline:.2%}")
    axis.set_xlim(0, 1.25)
    axis.set_xticks([0, 0.25, 0.5, 0.75, 1])
    axis.xaxis.set_major_formatter(PercentFormatter(1))
    axis.set_title("Three-field combinations, n >= 30 | 95% Wilson intervals")
    axis.set_xlabel("Failure rate (valid returned outcomes only)")
    axis.legend(loc="lower right")
    figure.savefig(figures / "02_top_combinations.png", dpi=170)
    plt.close(figure)


def rootcause(source, output, figures):
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    raw, data, parsed = load_data(source)
    feature_columns = ["data_lag_hours", "buffer_days", "msg_count"]
    buckets = bucket_features(parsed[feature_columns])
    outcome = parsed["result_ok"].copy()
    outcome.loc[data["result_ok"].notna() & outcome.isna()] = 2
    success, failed, missing, invalid, returned = outcome_counts(data)
    baseline = failed / returned if returned else math.nan
    singles = {key: summarize_groups(buckets, outcome, [key]) for key in BUCKET_LEVELS}
    combinations = summarize_groups(buckets, outcome, list(BUCKET_LEVELS))
    ranked = ranked_combinations(combinations)
    keep = ~raw.duplicated()
    dedup_combinations = summarize_groups(buckets.loc[keep], outcome.loc[keep], list(BUCKET_LEVELS))
    dedup_ranked = ranked_combinations(dedup_combinations)
    dedup_returned = int(outcome.loc[keep].isin([0, 1]).sum())
    dedup_failed = int(outcome.loc[keep].eq(0).sum())
    dedup_rate = dedup_failed / dedup_returned if dedup_returned else math.nan
    output.parent.mkdir(parents=True, exist_ok=True)
    for key, summary in singles.items():
        summary.to_csv(output.with_name(f"02_{key}.csv"), index=False, encoding="utf-8-sig")
    combinations.to_csv(output.with_name("02_combinations.csv"), index=False, encoding="utf-8-sig")
    dedup_combinations.to_csv(output.with_name("02_combinations_deduplicated.csv"), index=False, encoding="utf-8-sig")
    plot_rootcause(singles, ranked, baseline, figures)
    lines = [
        "# 根因分析：探索性风险关联，不是因果证明", "",
        "## 1. 复现与分析口径", "",
        "运行：`python -m pip install pandas matplotlib`，然后 `python scripts/analyze.py --stage rootcause`。", "",
        f"源文件 SHA-256：`{source_hash}`。Python {platform.python_version()}，pandas {pd.__version__}。", "",
        "审计依据：`01_audit.md`；唯一数据源为原始 `interventions.csv`，本次未修改。主分析保留原始行，附完全重复行去重敏感性分析。", "",
        f"全部 {len(data)} 行；有效回传 {returned} 行，其中失效 {failed}、遵从 {success}；未回传 {missing}、非法结果 {invalid}。", "",
        f"基线失效率 = {failed}/{returned} = {baseline:.2%}。各桶分母仅含 result_ok 为 0/1 的记录，空值不填补。", "",
        "分桶函数仅接收 data_lag_hours、buffer_days、msg_count；data_lag_hours 仅由两个输入时间计算。result_ok 仅在分桶完成后作为结果标签聚合，不作特征或规则输入。", "",
        "组合定义为上述三个字段桶的交叉组合，不按结果筛选桶，也不搜索额外阈值。排名按失效率降序；并列按有效回传数降序展示，报告保留最高值的全部并列组合。", "",
        "样本门槛 n >= 30 指有效回传数而非全部行数。95% Wilson 区间描述单桶比例不确定性，不是多重比较校正或因果区间。", "",
        "## 2. 桶边界", "",
        "- 延迟：负值 <0；<1h 为 [0,1)；1-6h 为 [1,6)；6-24h 为 [6,24)；1-3d 为 [24,72]；>3d 为 (72,+∞)，单位小时。恰好 1 小时进入 1-6h，恰好 3 天进入 1-3d。",
        "- 缓冲天数：<=1 为 [0,1]；2-3 为 (1,3]；4-7 为 (3,7]；>7 为 (7,+∞)。原始数据为整数，负值单列 invalid。",
        "- 消息量：0；1-2 为 (0,2]；3-5 为 (2,5]；>5 为 (5,+∞)。原始数据为整数，负值单列 invalid。",
        "- 特征缺失单列 missing，负延迟保留。零有效回传桶显示 NA，而不是 0% 失效。", "",
        "## 3. 单字段失效率", "",
    ]
    for key, summary in singles.items():
        lines += [f"### {key}", "", group_table(summary, [key]), ""]
    lines += ["## 4. 三字段交叉组合", "",
              f"完整预设组合数（含异常、缺失和空桶）为 {len(combinations)}；实际有记录的组合 {int(combinations['total'].gt(0).sum())}；满足有效回传数 >= 30 的组合 {len(ranked)}。", "",
              "完整表含空桶及小样本标记字段，见 `02_combinations.csv`；下表展示满足门槛的全部组合。", "",
              group_table(ranked, list(BUCKET_LEVELS)), ""]
    if ranked.empty:
        lines += ["没有满足门槛的组合，样本不足，不能下结论。", ""]
    else:
        winners = ranked.loc[ranked["failure_rate"].eq(ranked.iloc[0]["failure_rate"])]
        for _, winner in winners.iterrows():
            delta = (winner["failure_rate"] - baseline) * 100
            lines += [f"最高失效率组合：lag={winner['lag_bucket']}、buffer={winner['buffer_bucket']}、msg={winner['msg_bucket']}；"
                      f"失效 {winner['failed']}/{winner['returned']} = {winner['failure_rate']:.2%}，相对总体高 {delta:.2f} 个百分点。"
                      "这是样本内风险关联信号，不代表三个条件是失效原因，也不是已验证的上线规则。", ""]
    lines += ["## 5. 去重敏感性分析", "",
              f"仅在内存中移除完全重复行 {int((~keep).sum())} 条；保留 {int(keep.sum())} 行，有效回传 {dedup_returned}，失效 {dedup_failed}，失效率 {dedup_rate:.2%}。原始文件与主分析均不删除记录。", "",
              "去重后满足门槛的组合（同一组预设桶，不重新搜索阈值）：", "",
              group_table(dedup_ranked, list(BUCKET_LEVELS)), "",
              "完整去重组合表见 `02_combinations_deduplicated.csv`。完全重复不必然等于采集错误，业务去重口径仍需核实。", "",
              "## 6. 判断与限制", ""]
    for key, summary in singles.items():
        occupied = summary.loc[summary["total"].gt(0)]
        eligible = summary.loc[summary["eligible"]]
        lines += [f"- {key}：实际有记录 {len(occupied)} 桶，满足有效回传门槛 {len(eligible)} 桶。", ""]
        if len(eligible) < 2:
            lines += ["该字段不足两个可比较的达标桶，不能判断其风险梯度或独立作用。", ""]
        else:
            peak = eligible.sort_values("failure_rate", ascending=False).iloc[0]
            lines += [f"达标桶中最高为 {peak[key]}：{peak['failed']}/{peak['returned']} = {peak['failure_rate']:.2%}。"
                      "这是未经混杂调整的关联，不说明该字段有独立因果作用，也不代表风险随数值单调变化。", ""]
    lines += [
        "- 当前数据不能证明信息脱节导致失效。时间延迟范围和分桶覆盖有限，不能外推未覆盖时段，也不能为增强信号事后修改指定桶。",
        "- 消息多可能由患者原本风险高而触发，缓冲天数也可能反映既有病情；存在反向解释及中心、计划、日期等混杂，本次未做混杂控制。",
        "- 未回传机制未知，已回传样本可能有选择偏差；不能把未回传当遵从或失效。重复加权、单期数据、组合筛选和小样本均限制稳定性。",
        "- 样本不足，不能下结论：有效回传数 <30 的任何组合（包括无样本桶）。样本门槛达标也不等于统计显著或因果成立。",
        "- 下一步应核实结果回传和业务去重口径，结合中心/计划/日期分层与时间外验证，再决定是否试点人工核验；本次不估算已实现收益。", "",
        "## 7. 图表", "",
        "灰色表示有效回传数不足；NA 表示无可计算的失效率。图中区间为 95% Wilson 区间，n 为有效回传数。", "",
    ]
    import os
    for filename in ["02_bucket_failure_rates.png", "02_top_combinations.png"]:
        relative = Path(os.path.relpath(figures / filename, output.parent)).as_posix()
        lines += [f"![{filename}]({relative})", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    print(f"rootcause: rows={len(data)}, returned={returned}, failed={failed}, missing={missing}, baseline={baseline:.2%}")
    print(ranked[[*BUCKET_LEVELS, "returned", "failed", "failure_rate"]].to_string(index=False))
    print(f"Report: {output}")


RULE_INPUT_COLUMNS = ["created_at", "device_synced_at", "center_id", "plan_type", "buffer_days", "msg_count"]
RULE_TEXTS = {
    "buffer_2_3": "2 <= buffer_days <= 3",
    "buffer_le3": "0 <= buffer_days <= 3",
    "buffer_le7": "0 <= buffer_days <= 7",
    "prior_combination": "0 <= data_lag_hours < 1 AND 2 <= buffer_days <= 3 AND msg_count > 5",
    "all": "全部转人工（成本参照，不作为推荐）",
    "none": "全部不新增转人工（零增量成本参照）",
}


def rule_candidates(features):
    if set(features.columns) != set(RULE_INPUT_COLUMNS):
        raise ValueError("Rule inputs must contain only the six allowed pre-intervention fields.")
    buffer = features["buffer_days"]
    lag = (features["created_at"] - features["device_synced_at"]).dt.total_seconds() / 3600
    return {
        "buffer_2_3": buffer.ge(2) & buffer.le(3),
        "buffer_le3": buffer.ge(0) & buffer.le(3),
        "buffer_le7": buffer.ge(0) & buffer.le(7),
        "prior_combination": lag.ge(0) & lag.lt(1) & buffer.ge(2) & buffer.le(3) & features["msg_count"].gt(5),
        "all": pd.Series(True, index=features.index),
        "none": pd.Series(False, index=features.index),
    }


def evaluate_rule(name, mask, outcomes):
    mask = mask.fillna(False).astype(bool)
    hit_success, hit_failed, hit_missing, hit_invalid, hit_returned = outcome_counts(outcomes.loc[mask])
    other_success, other_failed, other_missing, other_invalid, other_returned = outcome_counts(outcomes.loc[~mask])
    hit_count = int(mask.sum())
    other_count = int((~mask).sum())
    hit_rate = hit_failed / hit_returned if hit_returned else math.nan
    other_rate = other_failed / other_returned if other_returned else math.nan
    total_returned = hit_returned + other_returned
    baseline = (hit_failed + other_failed) / total_returned if total_returned else math.nan
    hit_low, hit_high = wilson_interval(hit_failed, hit_returned)
    other_low, other_high = wilson_interval(other_failed, other_returned)
    return {
        "rule": name, "hits": hit_count, "hit_returned": hit_returned, "hit_failed": hit_failed,
        "hit_success": hit_success, "hit_missing": hit_missing, "hit_invalid": hit_invalid,
        "hit_rate": hit_rate, "nonhits": other_count, "nonhit_returned": other_returned,
        "nonhit_failed": other_failed, "nonhit_success": other_success,
        "nonhit_missing": other_missing, "nonhit_invalid": other_invalid, "nonhit_rate": other_rate,
        "lift_vs_nonhit": hit_rate / other_rate if other_rate > 0 else math.nan,
        "lift_vs_overall": hit_rate / baseline if baseline > 0 else math.nan,
        "minutes": hit_failed * 75 - hit_count * 10,
        "verification_cost": hit_count * 10, "hit_ci_low": hit_low, "hit_ci_high": hit_high,
        "nonhit_ci_low": other_low, "nonhit_ci_high": other_high,
        "sufficient": hit_returned >= 30 and other_returned >= 30,
    }


def rule_metrics_table(metrics):
    rows = []
    for _, row in metrics.iterrows():
        rate = lambda value: "NA" if pd.isna(value) else f"{value:.2%}"
        lift = "NA" if pd.isna(row["lift_vs_nonhit"]) else f"{row['lift_vs_nonhit']:.3f}"
        rows.append((row["rule"], row["hits"], row["hit_returned"], row["hit_failed"], row["hit_missing"],
                     rate(row["hit_rate"]), row["nonhits"], row["nonhit_returned"], row["nonhit_failed"],
                     rate(row["nonhit_rate"]), lift, row["minutes"],
                     "可探索比较" if row["sufficient"] else "样本不足，不能下结论"))
    return table(["规则/口径", "命中数", "命中有效回传", "命中失效", "命中未回传", "命中失效率",
                  "未命中数", "未命中有效回传", "未命中失效", "未命中失效率", "提升倍数（对未命中）",
                  "乐观净节省分钟", "样本说明"], rows)


def plot_rule(metrics, selected, scenarios, figures):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    figures.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(1, 3, figsize=(17, 5), constrained_layout=True)
    axis = axes[0]
    for position, prefix, label in [(0, "hit", "Hit"), (1, "nonhit", "Non-hit")]:
        value = selected[f"{prefix}_rate"]
        if pd.notna(value):
            axis.bar(position, value, width=0.55, color=["#ba4a38", "#267b74"][position])
            axis.vlines(position, selected[f"{prefix}_ci_low"], selected[f"{prefix}_ci_high"], color="#222222")
            axis.text(position, selected[f"{prefix}_ci_high"] + 0.025,
                      f"{value:.2%}\nn={selected[f'{prefix}_returned']}", ha="center", fontsize=9)
    axis.set_xticks([0, 1], ["Hit", "Non-hit"])
    axis.set_ylim(0, 1)
    axis.set_xlim(-0.6, 1.6)
    axis.yaxis.set_major_formatter(PercentFormatter(1))
    axis.set_title("Selected rule | valid outcomes | 95% Wilson CI")
    axis.set_ylabel("Historical failure rate")
    candidates = metrics.loc[~metrics["rule"].isin(["all", "none"])]
    axes[1].barh(candidates["rule"], candidates["minutes"], color="#267b74")
    axes[1].axvline(0, color="#333333", linewidth=1)
    axes[1].set_title("Candidate optimistic net savings (p=100%)")
    axes[1].set_xlabel("Minutes | all hits incur verification cost")
    axes[2].plot(scenarios["avoidance_fraction"], scenarios["net_minutes"], marker="o", color="#267b74")
    axes[2].axhline(0, color="#ba4a38", linestyle="--")
    axes[2].xaxis.set_major_formatter(PercentFormatter(1))
    axes[2].set_title("Selected rule | assumed effectiveness scenarios")
    axes[2].set_xlabel("Assumed fraction of historical hit failures avoided")
    axes[2].set_ylabel("Net minutes (not observed savings)")
    figure.savefig(figures / "03_rule_evaluation.png", dpi=170)
    plt.close(figure)


def rule_analysis(source, output, figures):
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    raw, data, parsed = load_data(source)
    features = parsed[RULE_INPUT_COLUMNS].copy()
    masks = rule_candidates(features)
    metrics = pd.DataFrame([evaluate_rule(name, mask, data) for name, mask in masks.items()])
    candidates = metrics.loc[~metrics["rule"].isin(["all", "none"]) & metrics["sufficient"]]
    if candidates.empty:
        raise ValueError("No candidate has at least 30 valid outcomes in both hit and non-hit groups.")
    selected = candidates.sort_values(["minutes", "hits", "rule"], ascending=[False, True, True]).iloc[0]
    selected_name = selected["rule"]
    selected_mask = masks[selected_name]
    keep = ~raw.duplicated()
    dedup = evaluate_rule("deduplicated", selected_mask.loc[keep], data.loc[keep])
    dates = features["created_at"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    observed_dates = sorted(dates.dropna().unique())
    date_groups = [observed_dates[:-2], observed_dates[-2:]] if len(observed_dates) > 2 else [observed_dates, []]
    checks = [evaluate_rule("full_sample", selected_mask, data), dedup]
    for label, date_group in zip(["earlier_days", "latest_2_days"], date_groups):
        subset = dates.isin(date_group)
        checks.append(evaluate_rule(label, selected_mask.loc[subset], data.loc[subset]))
    checks = pd.DataFrame(checks)
    scenarios = pd.DataFrame({"avoidance_fraction": [0, 0.25, 0.5, 0.75, 1]})
    scenarios["net_minutes"] = scenarios["avoidance_fraction"] * selected["hit_failed"] * 75 - selected["hits"] * 10
    break_even = selected["verification_cost"] / (selected["hit_failed"] * 75) if selected["hit_failed"] else math.nan
    output.parent.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output.with_name("03_rule_candidates.csv"), index=False, encoding="utf-8-sig")
    checks.to_csv(output.with_name("03_rule_sensitivity.csv"), index=False, encoding="utf-8-sig")
    scenarios.to_csv(output.with_name("03_rule_effectiveness.csv"), index=False, encoding="utf-8-sig")
    plot_rule(metrics, selected, scenarios, figures)
    hit_unknown = selected["hit_missing"] + selected["hit_invalid"]
    lines = [
        "# 提前转人工核验规则：离线候选方案", "",
        "## 1. 规则文本", "",
        f"推荐用于后续影子验证的候选：`{selected_name}`。", "",
        f"**在任务生成时，若 `{RULE_TEXTS[selected_name]}`，提前转人工核验；否则保持既有随访流程。**", "",
        "人工核验应确认当前用药/生活方式方案、近期健康数据及患者是否自行调整；本规则不是用药决策，不自动停药或更改治疗。",
        "缓冲天数缺失则不由该阈值自动命中，单独进入数据质量检查；未命中不等于患者安全，也不替代既有紧急处置机制。", "",
        "规则函数输入严格限制为 created_at、device_synced_at、center_id、plan_type、buffer_days、msg_count；未使用的字段不增加条件。result_ok 及业务编号均不传入规则函数。", "",
        "## 2. 候选设计与选择", "",
        "根据 02_rootcause.md 的缓冲天数信号，比较以下少量可解释候选，不搜索任意阈值，不训练复杂树：", "",
        table(["候选", "条件"], RULE_TEXTS.items()), "",
        "仅在非基准候选中，要求命中与未命中各至少 30 条有效回传，再按指定公式的样本内乐观净节省降序选择；同分时优先命中数较少者。", "",
        "候选来自已查看全量数据的根因分析，选择与评估共用同一批结果，有选择偏差。未进行结果字段泄漏，但并非独立验证或已证实收益。", "",
        "## 3. 评估口径", "",
        "- 命中数计全部命中记录，包含未回传；核验成本对每条命中记录收费。",
        "- 命中/未命中失效率均为各组 0/(0+1)，未回传和非法结果不进分母，也不作为失效。",
        "- 提升倍数 = 命中失效率 / 未命中失效率；未命中失效率为 0 或不可计算时显示 NA，不人为构造有限倍数。另提供相对总体倍数。",
        "- 预计节省分钟（指定公式） = 命中中原本失效数 × 75 - 全部命中数 × 10。它假设命中历史失效均能避免且每次节约完整 75 分钟，是乐观情景而非实际观察收益。", "",
        "## 4. 全量原始行评估", "",
        rule_metrics_table(metrics), "",
        f"选中规则：命中 {selected['hits']} 条，其中有效回传 {selected['hit_returned']}、历史失效 {selected['hit_failed']}、未回传 {selected['hit_missing']}、非法结果 {selected['hit_invalid']}。", "",
        f"命中失效率 {selected['hit_rate']:.2%}；未命中失效率 {selected['nonhit_rate']:.2%}；提升倍数（对未命中）{selected['lift_vs_nonhit']:.3f}；相对总体倍数 {selected['lift_vs_overall']:.3f}。", "",
        f"预计节省分钟（乐观） = {selected['hit_failed']} × 75 - {selected['hits']} × 10 = {selected['minutes']} 分钟。", "",
        f"命中中 {hit_unknown} 条结果未知记录的历史失效不可确定；不假设其失效，但核验成本已经全部计入。", "",
        "## 5. 去重与时间稳定性诊断", "",
        "规则一旦选定即冻结，下表不重新调阈值。去重仅在内存移除完全重复行，原始数据不修改。", "",
        rule_metrics_table(checks), "",
        f"按中国标准时间日期划分：较早日期 {', '.join(date_groups[0]) or '无'}；最后两日 {', '.join(date_groups[1]) or '无'}。", "",
        "日期分组仅为事后稳定性诊断：这些日期已参与之前探索与规则选择，绝不能称为独立留出集、时间外验证或无偏泛化评估。任一侧有效回传数不足 30 时，样本不足，不能下结论。", "",
        "## 6. 人工核验效果的不确定性", "",
        "下表中的 p 是假设的历史命中失效可避免比例，不是数据估计的干预效果；不对未回传补造结果。", "",
        "情景净节省 = p × 命中历史失效数 × 75 - 全部命中数 × 10。", "",
        table(["假设避免比例 p", "情景净节省分钟"], [(f"{row.avoidance_fraction:.0%}", row.net_minutes) for row in scenarios.itertuples(index=False)]), "",
        f"盈亏平衡 p = {selected['verification_cost']} / ({selected['hit_failed']} × 75) = {break_even:.2%}。实际效果未测量，不能据此承诺净节省。", "",
        "## 7. 局限与试点建议", "",
        "- 这是风险关联规则，不代表缓冲天数导致失效；原始风险不单调，不能将该候选解释为低于其区间的患者不紧迫。",
        "- 样本内选择、完全重复行、未回传、中心/计划/日期混杂及消息/延迟桶覆盖不足可能影响结果。有效回传数低于 30 的组必须标注样本不足，不能下结论。",
        "- 先影子运行记录真实核验工作量及回传，再通过前瞻性对照试点评估失效率、实际节约分钟、投诉和核验效果；不延迟既有高危患者服务。",
        "- 本次不新增临床处置规则，不把患者遵从标签当作未来可见输入，也不将乐观测算包装为已实现收益。", "",
        "## 8. 图表与复现", "",
        "运行：`python -m pip install pandas matplotlib`，然后 `python scripts/analyze.py --stage rule`。", "",
        f"输入 SHA-256：`{source_hash}`。Python {platform.python_version()}，pandas {pd.__version__}。", "",
        "明细见 03_rule_candidates.csv、03_rule_sensitivity.csv、03_rule_effectiveness.csv；失效率图的 n 为有效回传数，区间为 95% Wilson 区间（未校正选择和重复观测）。", "",
    ]
    import os
    relative = Path(os.path.relpath(figures / "03_rule_evaluation.png", output.parent)).as_posix()
    lines += [f"![规则评估与假设效果情景]({relative})", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    print(f"selected_rule={selected_name}: {RULE_TEXTS[selected_name]}")
    print(metrics[["rule", "hits", "hit_returned", "hit_failed", "hit_rate", "nonhit_rate", "lift_vs_nonhit", "minutes"]].to_string(index=False))
    print(f"Report: {output}")


def main():
    parser = argparse.ArgumentParser(description="Audit interventions without imputing missing outcomes.")
    parser.add_argument("--input", type=Path, default=ROOT / "interventions.csv")
    parser.add_argument("--stage", choices=["audit", "rootcause", "rule"], default="audit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--figures", type=Path, default=ROOT / "figures")
    args = parser.parse_args()
    filenames = {"audit": "01_audit.md", "rootcause": "02_rootcause.md", "rule": "03_rule.md"}
    output = args.output or ROOT / "reports" / filenames[args.stage]
    if args.input.resolve() in {output.resolve(), output.with_name("01_distributions.csv").resolve()}:
        parser.error("Output paths must not overwrite the input CSV.")
    if args.stage == "audit":
        audit(args.input, output)
    elif args.stage == "rootcause":
        rootcause(args.input, output, args.figures)
    else:
        rule_analysis(args.input, output, args.figures)


if __name__ == "__main__":
    main()