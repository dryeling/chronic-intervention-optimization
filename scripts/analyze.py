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


def validation_sample_note(total, returned):
    if total < 30:
        return "样本不足，不能下结论，需谨慎解读（总样本量 < 30）"
    if returned < 30:
        return "样本不足，不能下结论，需谨慎解读（有效回传数 < 30）"
    return "样本门槛达标，仅作探索性比较"


def validation_analysis(source, output):
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    _, data, parsed = load_data(source)
    rule_name = "buffer_2_3"
    hit = rule_candidates(parsed[RULE_INPUT_COLUMNS])[rule_name].fillna(False).astype(bool)
    metrics = evaluate_rule(rule_name, hit, data)
    total = len(data)
    total_failed = metrics["hit_failed"] + metrics["nonhit_failed"]
    coverage = metrics["hit_failed"] / total_failed if total_failed else math.nan
    verification_share = metrics["hits"] / total if total else math.nan
    missed = parsed["result_ok"].eq(0).fillna(False) & ~hit
    assert int(missed.sum()) == metrics["nonhit_failed"]
    overall = pd.DataFrame([{**metrics, "total": total, "total_failed": total_failed,
                             "failure_coverage": coverage, "verification_share": verification_share}])
    missed_rows = []
    buckets = bucket_features(parsed[["data_lag_hours", "buffer_days", "msg_count"]])
    for field in BUCKET_LEVELS:
        counts = buckets.loc[missed, field].value_counts(sort=False, dropna=False)
        assert int(counts.sum()) == metrics["nonhit_failed"]
        for bucket, count in counts.items():
            missed_rows.append({
                "field": field, "bucket": bucket, "missed_failed": int(count),
                "share_of_missed": int(count) / metrics["nonhit_failed"] if metrics["nonhit_failed"] else math.nan,
                "sample_note": validation_sample_note(int(count), int(count)),
            })
    missed_summary = pd.DataFrame(missed_rows)
    strata_rows = []
    comparisons = []
    for dimension in ["center_id", "plan_type"]:
        for label, group in data.groupby(dimension, dropna=False, sort=True):
            result = evaluate_rule(rule_name, hit.loc[group.index], group)
            comparisons.append({"dimension": dimension, "group": label,
                                "sufficient": result["sufficient"],
                                "higher_hit_rate": result["hit_rate"] > result["nonhit_rate"]})
            for prefix, arm, count in [("hit", "命中", result["hits"]), ("nonhit", "未命中", result["nonhits"])]:
                strata_rows.append({
                    "dimension": dimension, "group": "（缺失）" if pd.isna(label) else label,
                    "stratum_total": len(group), "arm": arm, "total": count,
                    "returned": result[f"{prefix}_returned"], "failed": result[f"{prefix}_failed"],
                    "success": result[f"{prefix}_success"], "missing": result[f"{prefix}_missing"],
                    "invalid": result[f"{prefix}_invalid"], "failure_rate": result[f"{prefix}_rate"],
                    "ci_low": result[f"{prefix}_ci_low"], "ci_high": result[f"{prefix}_ci_high"],
                    "stratum_note": validation_sample_note(len(group), result["hit_returned"] + result["nonhit_returned"]),
                    "sample_note": validation_sample_note(count, result[f"{prefix}_returned"]),
                })
    strata = pd.DataFrame(strata_rows)
    for dimension in ["center_id", "plan_type"]:
        summary = strata.loc[strata["dimension"].eq(dimension)]
        assert int(summary["total"].sum()) == total
        assert int(summary["failed"].sum()) == total_failed
        assert int(summary["missing"].sum()) == int(data["result_ok"].isna().sum())
        assert int(summary.loc[summary["arm"].eq("命中"), "total"].sum()) == metrics["hits"]
    assert (strata["total"] == strata[["returned", "missing", "invalid"]].sum(axis=1)).all()
    assert (strata["returned"] == strata[["failed", "success"]].sum(axis=1)).all()
    output.parent.mkdir(parents=True, exist_ok=True)
    overall.to_csv(output.with_name("04_coverage.csv"), index=False, encoding="utf-8-sig")
    missed_summary.to_csv(output.with_name("04_missed_failures.csv"), index=False, encoding="utf-8-sig")
    strata.to_csv(output.with_name("04_strata.csv"), index=False, encoding="utf-8-sig")
    percentage = lambda value: "NA" if pd.isna(value) else f"{value:.2%}"
    lines = [
        "# 迭代验证：规则覆盖能力与分层诊断", "",
        "## 口径与复现", "",
        f"冻结上一轮规则 `{rule_name}`：`{RULE_TEXTS[rule_name]}`；只新增诊断，不重选或放宽阈值。",
        "规则函数仅接收六个允许的干预前字段；result_ok 不进入规则，只用于命中之后的离线结果统计。", "",
        "唯一输入为 interventions.csv。主统计保留原始行及完全重复记录，与 03_rule.md 的全量口径一致；不修改输入数据。",
        "失效率只使用有效回传 0/1，空值保持未回传，不填为 0；非法非空结果单列。零分母的比例显示 NA。", "",
        "运行：`python scripts/analyze.py --stage validation`；依赖 pandas。新增明细：04_coverage.csv、04_missed_failures.csv、04_strata.csv。",
        f"Python {platform.python_version()}，pandas {pd.__version__}；输入 SHA-256：`{source_hash}`。", "",
        "## 1. 规则覆盖能力", "",
        table(["指标", "计算与结果"], [
            ("总样本量", total), ("历史失效总数", total_failed),
            ("命中总数（含未回传）", metrics["hits"]),
            ("命中有效回传 / 未回传 / 非法结果", f"{metrics['hit_returned']} / {metrics['hit_missing']} / {metrics['hit_invalid']}"),
            ("命中且历史失效", metrics["hit_failed"]), ("未命中且历史失效", metrics["nonhit_failed"]),
            ("失效覆盖率", f"{metrics['hit_failed']} / {total_failed} = {percentage(coverage)}"),
            ("核验比例", f"{metrics['hits']} / {total} = {percentage(verification_share)}"),
            ("命中失效率", f"{metrics['hit_failed']} / {metrics['hit_returned']} = {percentage(metrics['hit_rate'])}"),
            ("未命中失效率", f"{metrics['nonhit_failed']} / {metrics['nonhit_returned']} = {percentage(metrics['nonhit_rate'])}"),
        ]), "",
        "失效覆盖率的分母为全部已知历史失效，不是有效回传总数；核验比例的分母为全部记录。覆盖历史失效不代表核验已经避免了这些失效，未命中也不代表停止原随访流程。", "",
        "## 2. 未命中的历史失效分布", "",
        f"仅在规则命中判断完成后，筛选未命中且 result_ok=0 的 {int(missed.sum())} 行做描述。",
        "沿用 02_rootcause.md 的固定桶边界：延迟按 UTC 时间差计算，负值单列；缓冲天数与消息量仍使用原定区间。未回传不混入本节。",
        "每张表的占比分母均为所有未命中历史失效，不是该特征桶的全部记录，因此本节占比不是桶内失效率；不同特征表不可相加。", "",
    ]
    for field in BUCKET_LEVELS:
        summary = missed_summary.loc[missed_summary["field"].eq(field)]
        lines += [f"### {field}", "", table(["特征区间", "未命中历史失效数", "占所有未命中失效", "样本说明"],
                  [(row.bucket, row.missed_failed, percentage(row.share_of_missed), row.sample_note)
                   for row in summary.itertuples(index=False)]), ""]
        if summary["missed_failed"].gt(0).any():
            peak = summary.loc[summary["missed_failed"].idxmax()]
            lines += [f"数量最多的区间为 {peak['bucket']}：{peak['missed_failed']} 条，占未命中失效 {percentage(peak['share_of_missed'])}。"
                      "这里只描述漏覆盖的构成，不能据此判断该区间风险更高或直接改规则。", ""]
    lines += ["## 3. 分中心与分计划验证", "",
              "中心和计划分别分层，不是中心×计划交叉切分。每层列出命中、未命中两个子组，以及层总样本量、子组总样本量、有效回传分母和结果缺失数。",
              "若层或任一子组总样本量 <30，明确标注“样本不足，不能下结论，需谨慎解读”；即使总量达标，有效回传数 <30 也作同样警示。",
              "区间为单组比例的 95% Wilson 区间；它不是命中与未命中差异的显著性检验，也未校正重复观测与多重比较。", ""]
    for dimension in ["center_id", "plan_type"]:
        summary = strata.loc[strata["dimension"].eq(dimension)]
        rows = []
        for row in summary.itertuples(index=False):
            interval = f"{percentage(row.ci_low)} - {percentage(row.ci_high)}" if row.returned else "NA"
            rows.append((row.group, row.stratum_total, row.stratum_note, row.arm, row.total, row.returned,
                         row.failed, row.success, row.missing, row.invalid, percentage(row.failure_rate), interval, row.sample_note))
        lines += [f"### {dimension}", "", table([
            "中心/计划", "层总样本量", "层样本说明", "规则子组", "子组总样本量", "有效回传数",
            "失效数", "遵从数", "未回传数", "非法结果数", "失效率", "95% Wilson 区间", "子组样本说明",
        ], rows), ""]
        checks = [row for row in comparisons if row["dimension"] == dimension]
        lines += [f"本维度共 {len(checks)} 个层；命中与未命中均满足有效回传门槛的层有 {sum(row['sufficient'] for row in checks)} 个。"
                  f"样本内命中失效率高于未命中的层有 {sum(row['higher_hit_rate'] for row in checks)} 个；该计数不是一致性或因果证明。", ""]
    lines += [
        "## 4. 本轮判断与局限", "",
        f"当前规则的历史失效覆盖率为 {percentage(coverage)}，核验比例为 {percentage(verification_share)}；它是有限人力下的候选，不是覆盖全部失效的方案。",
        "分层命中样本不足时，不能因为总体关联或单层点估计较高，就认定规律在各中心、各计划均成立；也不能因小样本出现相反方向就断言规则无效。",
        "这是在同一份已用于规则设计的数据上做的事后诊断，不是独立验证，也未完成混杂调整。完全重复记录仍会加权；此前去重敏感性见 03_rule.md。",
        "后续应冻结规则收集新数据，在不改变既有高危处置的前提下前瞻性比较；单期数据不能推断真实核验效果、真实节约工时或未回传患者的失效情况。", "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    print(f"validation: rule={rule_name}, total={total}, hits={metrics['hits']}, total_failed={total_failed}, hit_failed={metrics['hit_failed']}, missed_failed={metrics['nonhit_failed']}")
    print(f"failure_coverage={percentage(coverage)}, verification_share={percentage(verification_share)}, strata_rows={len(strata)}")
    print(f"Report: {output}")


def minute_lag_buckets(lag):
    labels = pd.Series("missing", index=lag.index, dtype="string")
    intervals = [
        (lag.lt(0), "negative"),
        (lag.ge(0) & lag.lt(5 / 60), "0-5min"),
        (lag.ge(5 / 60) & lag.lt(15 / 60), "5-15min"),
        (lag.ge(15 / 60) & lag.lt(30 / 60), "15-30min"),
        (lag.ge(30 / 60) & lag.lt(1), "30-60min"),
        (lag.ge(1) & lag.lt(2), "1-2h"),
        (lag.ge(2) & lag.lt(6), "2-6h"),
        (lag.ge(6) & lag.lt(24), "6-24h"),
        (lag.ge(24), ">=24h"),
    ]
    for condition, label in intervals:
        labels.loc[condition.fillna(False)] = label
    return pd.Categorical(labels, categories=[label for _, label in intervals] + ["missing"], ordered=True)


def data_quality_analysis(source, output):
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    raw, data, parsed = load_data(source)
    success, failed, missing, invalid, returned = outcome_counts(data)
    missing_mask = data["result_ok"].isna()
    returned_mask = parsed["result_ok"].isin([0, 1])
    duplicate_mask = data["intervention_id"].notna() & data["intervention_id"].duplicated(keep=False)
    duplicate_rows = []
    duplicate_columns = [
        "intervention_id", "rows", "excess_rows", "raw_variants", "feature_variants",
        "raw_differing_fields", "parsed_differing_features", "outcome_variants",
    ]
    for identifier, group in data.loc[duplicate_mask].groupby("intervention_id", sort=True):
        original = raw.loc[group.index]
        features = parsed.loc[group.index, RULE_INPUT_COLUMNS]
        duplicate_rows.append({
            "intervention_id": identifier, "rows": len(group), "excess_rows": len(group) - 1,
            "raw_variants": len(original.drop_duplicates()), "feature_variants": len(features.drop_duplicates()),
            "raw_differing_fields": ", ".join(column for column in EXPECTED_COLUMNS if original[column].nunique(dropna=False) > 1),
            "parsed_differing_features": ", ".join(column for column in RULE_INPUT_COLUMNS if features[column].nunique(dropna=False) > 1),
            "outcome_variants": group["result_ok"].nunique(dropna=False),
        })
    duplicates = pd.DataFrame(duplicate_rows, columns=duplicate_columns)
    assert int(duplicates["rows"].sum()) == int(duplicate_mask.sum())
    assert int(duplicates["excess_rows"].sum()) == int(data.loc[data["intervention_id"].notna(), "intervention_id"].duplicated().sum())
    group_rows = []
    for dimension in ["center_id", "plan_type"]:
        for label, group in data.groupby(dimension, dropna=False, sort=True):
            group_success, group_failed, group_missing, group_invalid, group_returned = outcome_counts(group)
            group_rows.append({
                "dimension": dimension, "group": "（缺失）" if pd.isna(label) else label,
                "total": len(group), "missing": group_missing, "returned": group_returned,
                "success": group_success, "failed": group_failed, "invalid": group_invalid,
                "missing_rate": group_missing / len(group),
                "share_of_all_missing": group_missing / missing if missing else math.nan,
                "share_of_all_records": len(group) / len(data),
                "failure_rate_returned": group_failed / group_returned if group_returned else math.nan,
                "sample_note": validation_sample_note(len(group), group_returned),
                "missing_sample_note": validation_sample_note(group_missing, group_missing),
            })
    groups = pd.DataFrame(group_rows)
    for dimension in ["center_id", "plan_type"]:
        summary = groups.loc[groups["dimension"].eq(dimension)]
        assert int(summary["total"].sum()) == len(data)
        assert int(summary["missing"].sum()) == missing
        assert int(summary["returned"].sum()) == returned
    feature_rows = []
    for field in ["data_lag_hours", "buffer_days"]:
        unknown = parsed.loc[missing_mask, field]
        observed = parsed.loc[returned_mask, field]
        feature_rows.append({
            "feature": field, "missing_group_total": missing, "missing_valid_feature": int(unknown.count()),
            "missing_unusable_feature": int(unknown.isna().sum()), "missing_negative_feature": int(unknown.lt(0).sum()),
            "missing_mean": unknown.mean(), "missing_median": unknown.median(),
            "returned_group_total": returned, "returned_valid_feature": int(observed.count()),
            "returned_unusable_feature": int(observed.isna().sum()), "returned_negative_feature": int(observed.lt(0).sum()),
            "returned_mean": observed.mean(), "returned_median": observed.median(),
            "mean_difference": unknown.mean() - observed.mean(),
            "median_difference": unknown.median() - observed.median(),
            "sample_note": validation_sample_note(int(unknown.count()), int(observed.count())),
        })
    features = pd.DataFrame(feature_rows)
    minute_buckets = minute_lag_buckets(parsed["data_lag_hours"])
    minute_rows = []
    for label in minute_buckets.categories:
        group = data.loc[minute_buckets == label]
        group_success, group_failed, group_missing, group_invalid, group_returned = outcome_counts(group)
        ci_low, ci_high = wilson_interval(group_failed, group_returned)
        minute_rows.append({
            "minute_bucket": label, "total": len(group), "returned": group_returned,
            "failed": group_failed, "success": group_success, "missing": group_missing, "invalid": group_invalid,
            "failure_rate": group_failed / group_returned if group_returned else math.nan,
            "ci_low": ci_low, "ci_high": ci_high,
            "sample_note": validation_sample_note(len(group), group_returned),
        })
    minutes = pd.DataFrame(minute_rows)
    assert int(minutes["total"].sum()) == len(data)
    assert int(minutes["failed"].sum()) == failed
    assert int(minutes["missing"].sum()) == missing
    assert int(minutes["returned"].sum()) == returned
    assert (minutes["total"] == minutes[["returned", "missing", "invalid"]].sum(axis=1)).all()
    assert (groups["total"] == groups[["returned", "missing", "invalid"]].sum(axis=1)).all()
    deduplicated = data.loc[~raw.duplicated()]
    dedup_success, dedup_failed, dedup_missing, dedup_invalid, dedup_returned = outcome_counts(deduplicated)
    short_mask = parsed["data_lag_hours"].ge(0) & parsed["data_lag_hours"].lt(0.5)
    short_success, short_failed, short_missing, short_invalid, short_returned = outcome_counts(data.loc[short_mask.fillna(False)])
    urgent_missing = int(parsed.loc[missing_mask, "buffer_days"].between(0, 3).sum())
    urgent_returned = int(parsed.loc[returned_mask, "buffer_days"].between(0, 3).sum())
    urgent_missing_n = int(parsed.loc[missing_mask, "buffer_days"].count())
    urgent_returned_n = int(parsed.loc[returned_mask, "buffer_days"].count())
    qualified_minutes = minutes.loc[minutes["returned"].ge(30) & ~minutes["minute_bucket"].isin(["negative", "missing"])]
    minute_signal = "没有有效回传数达标的非负分钟桶，不能比较其失效率。"
    if not qualified_minutes.empty:
        highest = qualified_minutes.loc[qualified_minutes["failure_rate"].idxmax()]
        lowest = qualified_minutes.loc[qualified_minutes["failure_rate"].idxmin()]
        minute_signal = (
            f"在有效回传不少于 30 的非负桶中，样本内失效率最高的是 {highest['minute_bucket']}："
            f"{highest['failed']} / {highest['returned']} = {highest['failure_rate']:.2%}；"
            f"最低的是 {lowest['minute_bucket']}：{lowest['failed']} / {lowest['returned']} = {lowest['failure_rate']:.2%}。"
            "原先小时级分桶掩盖了小时以内的差异，值得在新数据中按预先固定边界验证；不能直接用本轮点估计设定上线阈值。"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    for filename, frame in [("05_duplicates.csv", duplicates), ("05_missing_by_group.csv", groups),
                            ("05_missing_features.csv", features), ("05_minute_lag.csv", minutes)]:
        frame.to_csv(output.with_name(filename), index=False, encoding="utf-8-sig")
    percentage = lambda value: "NA" if pd.isna(value) else f"{value:.2%}"
    lines = [
        "# 数据质量与选择偏差诊断", "", "## 口径与复现", "",
        "运行：`python scripts/analyze.py --stage data_quality`；唯一输入 interventions.csv，依赖 pandas。",
        f"Python {platform.python_version()}，pandas {pd.__version__}；源文件 SHA-256：`{source_hash}`。",
        "所有主表保留原始行，不填补任何空值。result_ok 空值是未回传，有结果组仅包含有效 0/1；非法非空值另列，不归为未回传。",
        f"原始记录 {len(data)} 条；有结果 {returned} 条（遵从 {success}、失效 {failed}），未回传 {missing} 条，非法结果 {invalid} 条。",
        "本轮不修改或重选规则，结果仅用于事后分组统计，不作为规则输入。数值描述排除该字段自身的缺失/解析失败，逐项给出有效数；负数保留并计数。", "",
        "## 1. 重复编号与记录一致性", "",
        table(["指标", "数量"], [
            ("缺失 intervention_id 行数", int(data["intervention_id"].isna().sum())),
            ("非空唯一编号数", data["intervention_id"].nunique()),
            ("重复编号个数", len(duplicates)), ("重复编号涉及行数（含首次）", int(duplicate_mask.sum())),
            ("重复编号多余行数（不含首次）", int(duplicates["excess_rows"].sum())),
            ("原始八列完全重复的多余行数", int(raw.duplicated().sum())),
            ("重复编号中原始八列完全一致的编号数", int(duplicates["raw_variants"].eq(1).sum())),
            ("重复编号中六个解析后特征不一致的编号数", int(duplicates["feature_variants"].gt(1).sum())),
            ("重复编号中结果（含缺失状态）不一致的编号数", int(duplicates["outcome_variants"].gt(1).sum())),
        ]), "",
        "编号按去除首尾空白后的字符串比较，缺失编号不视为同一业务编号；原始一致性逐项比较八列原文，保留时间文本与空白差异。特征一致性另比较 UTC 时间、解析后数值及类别这六个允许输入字段；原始不同而解析后相同也可在明细中识别。",
        "逐编号明细见 05_duplicates.csv（不同字段列表为空表示无差异）；以下展示前 20 个重复编号：", "",
        table(["intervention_id", "行数", "原始版本数", "特征版本数", "原始不同字段", "解析后不同特征", "结果版本数"],
              [(row.intervention_id, row.rows, row.raw_variants, row.feature_variants, row.raw_differing_fields,
                row.parsed_differing_features, row.outcome_variants) for row in duplicates.head(20).itertuples(index=False)]), "",
        f"仅供敏感性参照：内存中按原始八列完全去重后剩 {len(deduplicated)} 行，有效回传 {dedup_returned}、失效 {dedup_failed}、未回传 {dedup_missing}、非法 {dedup_invalid}。本节未覆写输入或改变后续主表口径。",
        "重复编号违反给定的唯一编号契约，但即使字段完全一致，也只能判断重复存储的可能性，不能仅凭本文件确认是导出重复还是业务重复；应核对上游唯一约束和事件日志。", "",
        "## 2. 未回传记录的中心与计划分布", "",
        "组内未回传率 = 该组未回传 / 该组全部记录；未回传构成比 = 该组未回传 / 全部未回传；总体构成比 = 该组总数 / 全部记录。不能仅按未回传条数认定集中，因为各组规模不同。",
        "回传失效率只描述各层已回传的 0/1，不用于推断未回传患者的结果。未回传子组不足 30 条时明确警示，不据小样本强行排序归因。", "",
    ]
    for dimension in ["center_id", "plan_type"]:
        summary = groups.loc[groups["dimension"].eq(dimension)]
        lines += [f"### {dimension}", "", table([
            "中心/计划", "总行数", "未回传数", "有效回传数", "非法结果数", "组内未回传率",
            "未回传构成比", "总体构成比", "回传失效数", "回传失效率", "总体样本说明", "未回传子组说明",
        ], [(row.group, row.total, row.missing, row.returned, row.invalid, percentage(row.missing_rate),
             percentage(row.share_of_all_missing), percentage(row.share_of_all_records), row.failed,
             percentage(row.failure_rate_returned), row.sample_note, row.missing_sample_note)
            for row in summary.itertuples(index=False)]), ""]
        if missing:
            peak = summary.loc[summary["missing_rate"].idxmax()]
            lines += [f"该维度样本内未回传率最高的是 {peak['group']}：{peak['missing']} / {peak['total']} = {percentage(peak['missing_rate'])}。"
                      f"{peak['missing_sample_note']}；不将最高点估计等同于稳定的人群差异。", ""]
    lines += ["## 3. 未回传与有结果组的特征差异", "",
              "差值统一为“未回传组 - 有结果组”；data_lag_hours 单位为小时，buffer_days 单位为天。无有效特征时显示 NA，不填补。", "",
              table(["特征", "未回传组总数", "特征有效数", "均值", "中位数", "有结果组总数", "特征有效数", "均值", "中位数", "均值差", "中位数差", "样本说明"],
                    [(row.feature, row.missing_group_total, row.missing_valid_feature, row.missing_mean, row.missing_median,
                      row.returned_group_total, row.returned_valid_feature, row.returned_mean, row.returned_median,
                      row.mean_difference, row.median_difference, row.sample_note) for row in features.itertuples(index=False)]), "",
              table(["特征", "未回传组特征缺失/解析失败", "有结果组特征缺失/解析失败", "未回传组负值", "有结果组负值"],
                    [(row.feature, row.missing_unusable_feature, row.returned_unusable_feature,
                      row.missing_negative_feature, row.returned_negative_feature) for row in features.itertuples(index=False)]), ""]
    for row in features.itertuples(index=False):
        direction = "无法比较" if pd.isna(row.mean_difference) else "更高" if row.mean_difference > 0 else "更低" if row.mean_difference < 0 else "相同"
        lines += [f"{row.feature}：未回传组均值相对有结果组{direction}，均值差 {format_value(row.mean_difference)}，中位数差 {format_value(row.median_difference)}。", ""]
    lines += [
        f"补充紧迫性构成（沿用 03_rule.md 已有的 0 <= buffer_days <= 3 范围，不新建规则）：未回传组 {urgent_missing} / {urgent_missing_n} = {percentage(urgent_missing / urgent_missing_n if urgent_missing_n else math.nan)}，有结果组 {urgent_returned} / {urgent_returned_n} = {percentage(urgent_returned / urgent_returned_n if urgent_returned_n else math.nan)}；分母分别为各组 buffer_days 可解析的记录数，缺失未填补。",
        "缓冲天数越小只表示业务定义中的紧迫性更高，不能等同于患者实际失效。均值/中位数、分层未回传率和上述构成比较可提示特定特征上的样本选择风险，但没有未回传患者的真实结果，无法确认其是否更高风险、偏差方向及幅度。",
        "本轮未进行混杂调整或缺失机制检验；均值接近不证明随机缺失，均值不同也不证明非随机缺失或因果效应。需补查回传链路、观察窗口与后续追访结果，不能仅根据已回传样本概括全部患者。", "",
        "## 4. 分钟级数据延迟与失效率", "",
        "延迟为 created_at - device_synced_at，带时区解析后统一为 UTC。非负桶均为左闭右开：如 30 分钟进入 30-60min，60 分钟进入 1-2h，2 小时进入 2-6h；>=24h 无上界。负值及缺失/无法计算单列，不删除。",
        "失效率 = 失效数 / 有效回传数；未回传和非法结果不进入分母。95% Wilson 区间仅为描述，未校正多重比较和重复记录；有效回传不足 30 时不能下结论。", "",
        table(["分钟桶", "总行数", "有效回传数", "失效数", "未回传数", "非法结果数", "失效率", "95% Wilson 区间", "样本说明"],
              [(row.minute_bucket, row.total, row.returned, row.failed, row.missing, row.invalid,
                percentage(row.failure_rate), f"{percentage(row.ci_low)} - {percentage(row.ci_high)}" if row.returned else "NA",
                row.sample_note) for row in minutes.itertuples(index=False)]), "",
        f"合并观察 0-30min（不与上表重复相加）：总行数 {int(short_mask.sum())}，有效回传 {short_returned}，失效 {short_failed}，未回传 {short_missing}，非法 {short_invalid}；失效率 {percentage(short_failed / short_returned if short_returned else math.nan)}。",
        minute_signal, "",
        "分钟桶揭示极短延迟内的描述性差异，不代表延迟导致失效，也不能将空桶理解为零风险。延迟本身不等于临床信息是否充分，更细分桶可能扩大偶然波动；本轮不据此调阈值。", "",
        "## 5. 局限与下一步", "",
        "所有主表按原始行计数；重复记录可能共同影响分布、均值和区间，记录也不必然等于独立患者。仅比较观测特征不能识别未回传结果或证明选择偏差不存在；应以新数据和补追访验证。",
        "明细文件：05_duplicates.csv、05_missing_by_group.csv、05_missing_features.csv、05_minute_lag.csv。记录数、结果分类和分钟桶守恒均由脚本断言检查。", "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    print(f"data_quality: rows={len(data)}, duplicate_ids={len(duplicates)}, duplicate_rows={int(duplicate_mask.sum())}, feature_conflicts={int(duplicates['feature_variants'].gt(1).sum())}, missing={missing}, returned={returned}")
    print(f"Report: {output}")


def budget_rule_mask(features, buffer_max, lag_min_minutes):
    if set(features.columns) != set(RULE_INPUT_COLUMNS):
        raise ValueError("Rule inputs must contain only the six allowed pre-intervention fields.")
    lag = (features["created_at"] - features["device_synced_at"]).dt.total_seconds() / 60
    return (features["buffer_days"].between(0, buffer_max) & lag.ge(lag_min_minutes)).fillna(False).astype(bool)


def select_budget_plans(candidates, total):
    rows = []
    for plan, share in [("A", 0.05), ("B", 0.25)]:
        cap = math.floor(total * share)
        pool = candidates.loc[candidates["kind"].isin(["threshold", "none"]) & candidates["hits"].le(cap)]
        adequate = pool.loc[pool["hit_returned"].ge(30)]
        if not adequate.empty:
            pool = adequate
        if pool["hit_failed"].eq(0).all():
            pool = candidates.loc[candidates["kind"].eq("none")]
        selected = pool.sort_values(["hit_failed", "hits", "rule"], ascending=[False, True, True]).iloc[0].to_dict()
        rows.append({**selected, "plan": plan, "budget_cap": cap, "coverage_target": math.nan})
    pool = candidates.loc[candidates["kind"].isin(["threshold", "all"]) & candidates["failure_coverage"].ge(0.95)]
    if pool.empty:
        pool = candidates.loc[candidates["kind"].eq("none")]
    selected = pool.sort_values(["hits", "hit_failed", "rule"], ascending=[True, False, True]).iloc[0].to_dict()
    rows.append({**selected, "plan": "C", "budget_cap": math.nan, "coverage_target": 0.95})
    result = pd.DataFrame(rows)
    assert result.loc[result["plan"].isin(["A", "B"]), "hits"].le(result.loc[result["plan"].isin(["A", "B"]), "budget_cap"]).all()
    return result


def plot_budget_tradeoff(candidates, frontier, selected, figures):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    figures.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(10, 6), constrained_layout=True)
    valid = candidates.loc[candidates["failure_coverage"].notna()]
    for adequate, color, label in [(False, "#b7b7b7", "Candidates: fewer than 30 valid hit outcomes"),
                                    (True, "#27857a", "Candidates: at least 30 valid hit outcomes")]:
        points = valid.loc[valid["hit_returned"].ge(30).eq(adequate)]
        axis.scatter(points["verification_share"], points["failure_coverage"], s=24, color=color, alpha=0.45, label=label)
    axis.step(frontier["verification_share"], frontier["failure_coverage"], where="post", color="#333333",
              linewidth=1.6, label="In-sample grid coverage envelope (not a causal effect)")
    for row, color, offset in zip(selected.itertuples(index=False), ["#b84b35", "#2466a0", "#775c20"],
                                  [(12, 12), (12, -35), (-115, -45)]):
        if pd.notna(row.failure_coverage):
            axis.scatter(row.verification_share, row.failure_coverage, s=95, color=color, edgecolors="white", zorder=5)
            axis.annotate(f"{row.plan}: {row.hits} checks\n{row.failure_coverage:.1%} coverage",
                          (row.verification_share, row.failure_coverage), xytext=offset,
                          textcoords="offset points", fontsize=10, color=color,
                          arrowprops={"arrowstyle": "-", "color": color})
    axis.xaxis.set_major_formatter(PercentFormatter(1))
    axis.yaxis.set_major_formatter(PercentFormatter(1))
    axis.set(xlim=(-0.02, 1.03), ylim=(-0.03, 1.06), xlabel="Verification count / all records (including missing outcomes)",
             ylabel="Historical failures hit / all known historical failures", title="Human verification budget vs. historical failure coverage")
    axis.grid(alpha=0.18)
    axis.legend(loc="lower right", fontsize=8)
    figure.savefig(figures / "budget_tradeoff.png", dpi=170)
    plt.close(figure)


def budget_tradeoff_analysis(source, output, figures):
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    _, data, parsed = load_data(source)
    _, total_failed, missing, invalid, returned = outcome_counts(data)
    total = len(data)
    rows = []
    inputs = parsed[RULE_INPUT_COLUMNS]
    for buffer_max in range(31):
        for lag_min in [0, 5, 10, 15, 20, 30, 45, 60, 120]:
            name = f"buffer_le{buffer_max}_lag_ge{lag_min}min"
            metrics = evaluate_rule(name, budget_rule_mask(inputs, buffer_max, lag_min), data)
            rows.append({**metrics, "kind": "threshold", "buffer_max": buffer_max, "lag_min_minutes": lag_min,
                         "condition": f"0 <= buffer_days <= {buffer_max} AND data_lag_hours >= {lag_min / 60:g} (lag >= {lag_min}min)"})
    for name in ["none", "all", "buffer_2_3"]:
        metrics = evaluate_rule(name, rule_candidates(inputs)[name], data)
        rows.append({**metrics, "kind": name, "buffer_max": math.nan, "lag_min_minutes": math.nan,
                     "condition": RULE_TEXTS[name]})
    candidates = pd.DataFrame(rows)
    candidates["verification_share"] = candidates["hits"] / total if total else math.nan
    candidates["failure_coverage"] = candidates["hit_failed"] / total_failed if total_failed else math.nan
    candidates["missed_failed"] = total_failed - candidates["hit_failed"]
    candidates["net_minutes_p50"] = candidates["hit_failed"] * 75 * 0.5 - candidates["verification_cost"]
    candidates["break_even_p"] = candidates["verification_cost"] / (candidates["hit_failed"] * 75).replace(0, math.nan)
    candidates["sample_note"] = [validation_sample_note(int(row.hits), int(row.hit_returned))
                                 for row in candidates.itertuples(index=False)]
    assert (candidates["hits"] + candidates["nonhits"] == total).all()
    assert (candidates["hit_failed"] + candidates["nonhit_failed"] == total_failed).all()
    assert (candidates["hit_missing"] + candidates["nonhit_missing"] == missing).all()
    assert (candidates["minutes"] == candidates["hit_failed"] * 75 - candidates["hits"] * 10).all()
    ordered = candidates.sort_values(["hits", "hit_failed", "rule"], ascending=[True, False, True]).drop_duplicates("hits")
    frontier = ordered.loc[ordered["hit_failed"].gt(ordered["hit_failed"].cummax().shift(fill_value=-1))].copy()
    assert frontier["hits"].is_monotonic_increasing and frontier["hit_failed"].is_monotonic_increasing
    selected = select_budget_plans(candidates, total)
    scenarios = pd.DataFrame([
        {"plan": row.plan, "rule": row.rule, "avoidance_fraction": fraction,
         "gross_minutes": row.hit_failed * 75 * fraction, "verification_minutes": row.verification_cost,
         "net_minutes": row.hit_failed * 75 * fraction - row.verification_cost}
        for row in selected.itertuples(index=False) for fraction in [0, 0.25, 0.5, 0.75, 1]
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    for filename, frame in [("06_budget_candidates.csv", candidates), ("06_budget_frontier.csv", frontier),
                            ("06_budget_selected.csv", selected), ("06_budget_effectiveness.csv", scenarios)]:
        frame.to_csv(output.with_name(filename), index=False, encoding="utf-8-sig")
    plot_budget_tradeoff(candidates, frontier, selected, figures)
    percentage = lambda value: "NA" if pd.isna(value) else f"{value:.2%}"
    import os
    relative_figure = Path(os.path.relpath(figures / "budget_tradeoff.png", output.parent)).as_posix()
    lines = [
        "# 人工核验预算与历史失效覆盖权衡", "", "## 1. 复现与假设", "",
        "运行：`python scripts/analyze.py --stage budget_tradeoff`；依赖 pandas、matplotlib，唯一输入 interventions.csv。",
        f"环境：Python {platform.python_version()}、pandas {pd.__version__}；源文件 SHA-256：`{source_hash}`。",
        f"主口径为原始 {total} 行，其中有效回传 {returned}、已知失效 {total_failed}、未回传 {missing}、非法结果 {invalid}。保留重复行及所有结果空值，不填补。",
        "规则函数只接收六个允许的干预前字段，result_ok 仅用于规则判断完成后的历史评估。负缓冲、负延迟或无法计算的特征不命中阈值规则，但仍保留在总体分母与单列计数中；全部核验参照包括所有记录。",
        table(["阈值规则无法命中的数据质量标记（可重叠）", "记录数"], [
            ("buffer_days 负值", int(parsed["buffer_days"].lt(0).sum())),
            ("buffer_days 缺失/解析失败", int(parsed["buffer_days"].isna().sum())),
            ("data_lag_hours 负值", int(parsed["data_lag_hours"].lt(0).sum())),
            ("data_lag_hours 缺失/无法计算", int(parsed["data_lag_hours"].isna().sum())),
        ]), "",
        "核验量按全部命中记录计数，未回传也消耗人工；核验比例 = 命中量 / 总记录数。历史失效覆盖率 = 命中且 result_ok=0 / 全部 result_ok=0，与命中失效率不同。无已知失效时覆盖率为 NA，不能据此优选。",
        "题目成本假设：每次核验 10 分钟，每次失效损失 75 分钟。设 p 为核验能真正避免命中历史失效的比例，净节省分钟 = p × 命中历史失效数 × 75 - 核验量 × 10。",
        "p 未被当前数据测量；表中 p=50% 只是对比情景，不是效果估计，p=100% 是乐观上限，p=0 则只有新增成本。历史覆盖不等于已避免失效，也没有把未回传当失效或遵从。", "",
        "## 2. 阈值遍历与选择口径", "",
        "阈值族为 `0 <= buffer_days <= X AND data_lag_hours >= Y`。X 遍历 0 至 30 的全部整数天；Y 分钟遍历 0、5、10、15、20、30、45、60、120。边界均包含等号。",
        f"实际评估 {int(candidates['kind'].eq('threshold').sum())} 种阈值组合；另含不新增核验、全部核验及原 buffer_2_3 规则参照，共 {len(candidates)} 行，均见 06_budget_candidates.csv。相同命中集合的不同阈值仍保留，不能视为独立证据。",
        "A/B 是规划用示例预算：总量的 5%/25%，向下取整；在不超预算且命中有效回传至少 30 的候选中优先最大化覆盖，相同覆盖取更少核验，仍相同按规则名确定性排序。若无达标候选则退回预算内方案并警示，绝不扩大预算。",
        "若预算内筛选后的候选均未覆盖任何已知失效，则返回不新增核验参照，不为达到样本门槛而增加无历史覆盖收益的核验。",
        "C 是覆盖优先：选择达到样本内至少 95% 历史失效覆盖的最少核验方案，同量时优先覆盖高者；可用全部核验参照兜底。95% 是规划目标，不是未来覆盖保证。没有已知失效则仅展示不新增核验参照。",
        "B 表示预算内覆盖优先的折中展示，不等于经济最优。原 buffer_2_3 仅作参照，不参与新方案选优；本阶段不替换原规则，所选规则未必相互嵌套。", "",
        "## 3. 三个典型方案", "",
        table(["方案", "定位", "规则", "预算上限/覆盖目标", "核验量", "核验比例", "命中有效回传", "命中失效", "命中未回传", "覆盖率", "漏掉失效", "命中失效率", "核验工时（小时）", "净节省分钟 p=50%（假设）", "乐观净节省分钟 p=100%", "盈亏平衡 p", "样本说明"],
              [(row.plan, {"A": "极低预算", "B": "中等预算", "C": "高预算/覆盖优先"}[row.plan], row.condition,
                f"{int(row.budget_cap)} 条" if pd.notna(row.budget_cap) else f"覆盖至少 {percentage(row.coverage_target)}",
                row.hits, percentage(row.verification_share), row.hit_returned, row.hit_failed, row.hit_missing,
                percentage(row.failure_coverage), row.missed_failed, percentage(row.hit_rate), row.verification_cost / 60,
                row.net_minutes_p50, row.minutes, percentage(row.break_even_p), row.sample_note)
               for row in selected.itertuples(index=False)]), "",
        "A 的“极端风险”只指紧迫缓冲与延迟阈值共同筛出的样本内高风险候选，不等同于临床风险评分；样本门槛达标也不代表稳定或显著。盈亏平衡 p = 核验成本 / (命中历史失效 × 75)，若超过 100% 则在本成本假设下即使完全有效也不划算；零命中失效时显示 NA。", "",
        "### 有效性情景（不是实际节省）", "",
        table(["方案", "假设避免比例 p", "假设避免工时（分钟）", "核验成本（分钟）", "假设净节省（分钟）"],
              [(row.plan, percentage(row.avoidance_fraction), row.gross_minutes, row.verification_minutes, row.net_minutes)
               for row in scenarios.itertuples(index=False)]), "",
        "## 4. 权衡曲线", "", f"![预算与覆盖权衡]({relative_figure})", "",
        "横轴为核验量 / 全部记录，纵轴为覆盖的已知历史失效比例。每个点是一个离散候选；灰点命中有效回传不足 30。阶梯线是已遍历候选的样本内覆盖上包络：同核验量保留最高覆盖，再剔除被更小核验量达到相同或更高覆盖的点，明细见 06_budget_frontier.csv。",
        "上包络不是总体经济最优、连续阈值插值或未来保证，也不表示相邻规则可以按相同比例扩容。全部核验为覆盖/成本参照，并非默认推荐。", "",
        "## 5. 给主管的选择建议", "",
        "先按与历史样本相同的任务周期和总量，核算健康管理师在保留原随访、高危处置及必要余量后，还剩多少可用于新增核验的分钟数；除以 10 并向下取整，就是可承受核验量 K。不要按现有总工作时长全部充当新增预算，也不要把本样本的累计工时当成每天或每人的工时。",
        "用 K 对照表中核验量和核验工时：资源很紧时先将 A 作为小规模影子运行候选；能承担 B 时，结合相对 A 增加的覆盖和工时再决定是否扩大；C 只适用于覆盖目标确实优先、容量足够且愿意接受相应成本的情况。核验量超过 K 的方案不可直接执行；若 A 也超预算，应暂不新增或在候选明细中另选不超 K 的规则，不按 result_ok 排队截取患者。",
        "选择前还须看 p=50% 的情景净节省和盈亏平衡 p：不能只因覆盖更高就选 C。净节省为负不代表临床上不值得，但意味着本工时模型下需要额外预算或其他明确业务价值；真实 p 只能通过前瞻性试点获得。",
        "上线前需按中心、计划和时间段核实峰值容量，补查重复记录和未回传机制；先冻结规则做影子运行，再用实测核验工时与真实结果更新成本和效果。三个方案都不应减少未命中患者的既有服务或替代紧急处置。", "",
        "## 6. 局限与明细", "",
        "阈值包含已从同一数据观察到的分钟级信号；遍历及按历史失效选优存在多重比较和样本内乐观偏差，不是独立验证，也不能证明延迟或缓冲天数导致失效。中心/计划混杂、小子组、未回传选择风险和重复加权仍存在，见 04_validation.md、05_data_quality.md。",
        "本阶段沿用原始行口径以便与前文对照，尚未对整个网格做去重/分层/未来数据验证。预算比例是规划假设，实际健康管理师人数、可用工时、峰值到达率及真实核验效果均未提供，不能据此承诺上线收益或人员编制。",
        "明细：06_budget_candidates.csv（全部候选）、06_budget_frontier.csv（覆盖上包络）、06_budget_selected.csv（三方案）、06_budget_effectiveness.csv（有效性情景）。", "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    print(f"budget_tradeoff: candidates={len(candidates)}, frontier={len(frontier)}, total={total}, failed={total_failed}")
    print(selected[["plan", "condition", "hits", "hit_failed", "failure_coverage", "net_minutes_p50", "minutes"]].to_string(index=False))
    print(f"Report: {output}; figure: {figures / 'budget_tradeoff.png'}")


def main():
    parser = argparse.ArgumentParser(description="Audit interventions without imputing missing outcomes.")
    parser.add_argument("--input", type=Path, default=ROOT / "interventions.csv")
    parser.add_argument("--stage", choices=["audit", "rootcause", "rule", "validation", "data_quality", "budget_tradeoff"], default="audit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--figures", type=Path, default=ROOT / "figures")
    args = parser.parse_args()
    filenames = {"audit": "01_audit.md", "rootcause": "02_rootcause.md", "rule": "03_rule.md", "validation": "04_validation.md", "data_quality": "05_data_quality.md", "budget_tradeoff": "06_budget_tradeoff.md"}
    output = args.output or ROOT / "reports" / filenames[args.stage]
    if args.input.resolve() in {output.resolve(), output.with_name("01_distributions.csv").resolve()}:
        parser.error("Output paths must not overwrite the input CSV.")
    if args.stage == "audit":
        audit(args.input, output)
    elif args.stage == "rootcause":
        rootcause(args.input, output, args.figures)
    elif args.stage == "rule":
        rule_analysis(args.input, output, args.figures)
    elif args.stage == "validation":
        validation_analysis(args.input, output)
    elif args.stage == "data_quality":
        data_quality_analysis(args.input, output)
    else:
        budget_tradeoff_analysis(args.input, output, args.figures)


if __name__ == "__main__":
    main()