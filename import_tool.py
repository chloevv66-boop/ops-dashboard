#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BigQuery 报表导入工具（Claude 交接版）
- 保留原有清洗逻辑
- 为会员报表补 `_snapshot_month` / `_snapshot_date`
- 为 TOP 报表补 `_snapshot_date`
- 为所有导入补 `_source_file`
"""

from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import sys
import traceback
from io import StringIO
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
from google.cloud import bigquery

PROJECT_ID = 'dashboard-492601'
DATASET_ID = 'ops_data'

FILE_MAP = [
    (r'business-report', 'raw_platform_report', '平台报表'),
    (r'finance-report', 'raw_finance_report', '财务报表'),
    (r'game-analysis', 'raw_game_analysis', '游戏分析'),
    (r'onlineBet-report|onlinebet', 'raw_realtime_bet', '实时投注监控'),
    (r'onlineRecharge|实时存取款|deposit-withdraw', 'raw_realtime_deposit_withdraw', '实时存取款监控'),
    (r'^top([\s\-\(.]|$)|top报表', 'raw_top_report', 'Top报表'),
    (r'会员', 'raw_member_report', '会员报表'),
    (r'日期.+一级|推广', 'raw_promotion_report', '推广报表'),
    (r'日期.+代理|代理报表', 'raw_agent_report', '代理报表'),
    (r'游戏报表.*场馆|游戏报表\(场馆\)', 'raw_game_report_venue', '游戏报表_场馆'),
    (r'游戏报表(?!.*场馆)', 'raw_game_report', '游戏报表'),
]

SKIP_EXACT = {
    '日期', '时间', '代理ID', '代理编号', '代理名称', '代理类型',
    '会员账号', '注册时间', '用户名', '站点名称', '场馆名称',
    '游戏类型', '游戏名称', '注册来源', '注册网址', '区号', '地区名称',
    '用户标签', '会员状态', '用户来源', '是否为代理', 'VIP等级',
    '推广渠道', '一级', '二级', '三级', '四级',
    '_snapshot_month', '_snapshot_date', '_imported_at', '_source_file',
}


def identify_report_type(filename: str) -> Tuple[Optional[str], Optional[str]]:
    basename = os.path.basename(filename)
    for pattern, table, display in FILE_MAP:
        if re.search(pattern, basename, re.IGNORECASE):
            return table, display
    return None, None


def detect_encoding(filepath: str) -> str:
    for enc in ['utf-8-sig', 'utf-8', 'gb18030', 'gbk', 'big5', 'latin-1']:
        try:
            with open(filepath, 'r', encoding=enc) as f:
                f.read(8192)
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return 'utf-8'


def clean_eq_value(val):
    if isinstance(val, str):
        m = re.match(r'^="(.*)"$', val)
        if m:
            return m.group(1)
    return val


def read_file(filepath: str) -> pd.DataFrame:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ('.xlsx', '.xls'):
        return pd.read_excel(filepath, engine='openpyxl', dtype=str)
    if ext == '.csv':
        enc = detect_encoding(filepath)
        with open(filepath, 'r', encoding=enc) as f:
            raw_lines = f.readlines()
        cleaned_lines = [line.rstrip('\n\r').rstrip(',') + '\n' for line in raw_lines]
        df = pd.read_csv(StringIO(''.join(cleaned_lines)), dtype=str, on_bad_lines='warn')
        df = df.apply(lambda col: col.map(clean_eq_value))
        df.columns = [clean_eq_value(c) for c in df.columns]
        return df
    raise ValueError(f'不支持的文件格式: {ext}')


def is_summary_row(row) -> bool:
    for val in row:
        if isinstance(val, str) and re.search(r'^(总计|合计|小计)$', val.strip()):
            return True
    return False


def is_all_zero_data_row(row, df_columns) -> bool:
    """
    判断一行是否应视为空数据行。
    规则：
    1. 排除标识/元数据列后，若所有数值列都为 0 或空，则视为空行
    2. 但推广报表中，若「单元代理人数」> 0，则该行应保留
       即使其他业务字段都为 0，也不当作空行过滤
    """
    # 这些字段只要 > 0，就代表该行有业务/组织意义，不能当空行过滤
    preserve_if_positive = {'单元代理人数'}

    # 先看是否命中"必须保留"字段
    for i, val in enumerate(row):
        col_name = str(df_columns[i]).strip() if i < len(df_columns) else ''
        if col_name in preserve_if_positive:
            if val is None:
                continue
            sval = str(val).strip()
            if sval in ('', 'None', 'nan', 'null'):
                continue
            try:
                if float(sval) > 0:
                    return False
            except (ValueError, TypeError):
                pass

    # 常规全零判断
    check_vals = []
    for i, val in enumerate(row):
        col_name = str(df_columns[i]).strip() if i < len(df_columns) else ''
        if col_name in SKIP_EXACT:
            continue
        if val is None or (isinstance(val, str) and val.strip() in ('', 'None', 'nan', 'null')):
            check_vals.append(0.0)
            continue
        try:
            check_vals.append(float(str(val).strip()))
        except (ValueError, TypeError):
            pass
    return bool(check_vals) and all(v == 0.0 for v in check_vals)


def extract_top_month(filename: str) -> Optional[str]:
    basename = os.path.basename(filename)
    m = re.search(r'(\d{6})', basename)
    return m.group(1) if m else None


def infer_member_snapshot_month(df: pd.DataFrame, filepath: str) -> Optional[str]:
    # 1) filename
    m = re.search(r'(20\d{2})[-_]?([01]\d)', os.path.basename(filepath))
    if m:
        return f"{m.group(1)}{m.group(2)}"

    # 2) max 注册时间 month (most reliable in current raw member files)
    if '注册时间' in df.columns:
        reg = pd.to_datetime(df['注册时间'].astype(str), errors='coerce')
        if reg.notna().any():
            dt = reg.max()
            return dt.strftime('%Y%m')

    # 3) file modified time fallback
    ts = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
    return ts.strftime('%Y%m')


def month_to_snapshot_date(yyyymm: Optional[str]) -> Optional[str]:
    if not yyyymm or not re.fullmatch(r'\d{6}', yyyymm):
        return None
    return f'{yyyymm[:4]}-{yyyymm[4:6]}-01'


def save_cleaned_full(df: pd.DataFrame, output_path: str) -> None:
    import xlsxwriter
    workbook = xlsxwriter.Workbook(output_path)
    worksheet = workbook.add_worksheet('Sheet1')
    header_fmt = workbook.add_format({'bold': True, 'bg_color': '#4472C4', 'font_color': 'white', 'border': 1})
    normal_fmt = workbook.add_format({'border': 1})
    grey_fmt = workbook.add_format({'bg_color': '#D9D9D9', 'font_color': '#999999', 'border': 1})

    for col_idx, col_name in enumerate(df.columns):
        worksheet.write(0, col_idx, str(col_name), header_fmt)
    worksheet.freeze_panes(1, 0)

    for row_idx in range(len(df)):
        row_data = df.iloc[row_idx]
        fmt = grey_fmt if is_all_zero_data_row(row_data.values, df.columns) else normal_fmt
        for col_idx in range(len(df.columns)):
            val = row_data.iloc[col_idx]
            if pd.isna(val) or val is None:
                worksheet.write(row_idx + 1, col_idx, '', fmt)
            else:
                sval = str(val).strip()
                try:
                    num = float(sval)
                    if num == int(num) and '.' not in sval and 'E' not in sval.upper():
                        worksheet.write_number(row_idx + 1, col_idx, int(num), fmt)
                    else:
                        worksheet.write_number(row_idx + 1, col_idx, num, fmt)
                except (ValueError, TypeError):
                    worksheet.write_string(row_idx + 1, col_idx, sval, fmt)
    worksheet.autofilter(0, 0, len(df), len(df.columns) - 1)
    workbook.close()


def _check_existing_source_file(table_name: str, source_file: str) -> int:
    """检查 BQ 表中是否已有该 _source_file 的记录,返回数量。表不存在或查询失败 → 0。"""
    try:
        client = bigquery.Client(project=PROJECT_ID)
        sql = (
            f"SELECT COUNT(*) AS n FROM `{PROJECT_ID}.{DATASET_ID}.{table_name}` "
            f"WHERE _source_file = @sf"
        )
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter('sf', 'STRING', source_file)
        ])
        result = list(client.query(sql, job_config=job_config).result())
        return result[0].n if result else 0
    except Exception:
        return 0


def upload_to_bigquery(df: pd.DataFrame, table_name: str, dry_run: bool = False, source_file: Optional[str] = None):
    if dry_run:
        return len(df)

    # 重复检查 — 同一个 _source_file 已经写过就跳过 (避免跑两次 = 数据 × 2)
    if source_file:
        existing = _check_existing_source_file(table_name, source_file)
        if existing > 0:
            print(f'    ⚠️  已有 {existing} 笔来自 "{source_file}" 在 {table_name} → 跳过 (避免重复写入)')
            return 0

    client = bigquery.Client(project=PROJECT_ID)
    table_id = f'{PROJECT_ID}.{DATASET_ID}.{table_name}'
    payload = df.copy()
    payload['_imported_at'] = pd.Timestamp.now()
    if source_file:
        payload['_source_file'] = source_file
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        autodetect=True,
        # 容忍后台月报新增栏位(如 4 月游戏报表多出「内嵌类型名」),自动加进表 schema,
        # 旧月份该栏为 NULL。否则 WRITE_APPEND 会因 schema 不一致整批失败。
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    job = client.load_table_from_dataframe(payload, table_id, job_config=job_config)
    job.result()
    return job.output_rows


def process_folder(folder_path: str, dry_run: bool = False):
    files = []
    for ext in ('*.xlsx', '*.xls', '*.csv'):
        files.extend(glob.glob(os.path.join(folder_path, ext)))
    files.sort()
    if not files:
        print(f'\n  文件夹中没有找到 XLSX/CSV 文件: {folder_path}')
        return

    cleaned_full_dir = os.path.join(folder_path, 'cleaned_full')
    cleaned_data_dir = os.path.join(folder_path, 'cleaned_data')
    os.makedirs(cleaned_full_dir, exist_ok=True)
    os.makedirs(cleaned_data_dir, exist_ok=True)

    summary = {}
    errors = []
    total_files = len(files)
    print(f'\n  找到 {total_files} 个文件，开始处理...\n')

    for idx, filepath in enumerate(files, 1):
        basename = os.path.basename(filepath)
        print(f'[{idx}/{total_files}] {basename}')
        try:
            table_name, display_name = identify_report_type(filepath)
            if table_name is None:
                print('  类型: 未识别，跳过')
                errors.append((basename, '无法识别文件类型'))
                print()
                continue
            print(f'  类型: {display_name}')

            df = read_file(filepath)
            original_rows = len(df)
            summary_mask = df.apply(lambda row: is_summary_row(row.values), axis=1)
            summary_count = int(summary_mask.sum())
            df_clean = df[~summary_mask].copy()
            zero_mask = df_clean.apply(lambda row: is_all_zero_data_row(row.values, df_clean.columns), axis=1)
            zero_count = int(zero_mask.sum())
            df_data = df_clean[~zero_mask].copy()

            # Add snapshot fields where applicable
            if table_name == 'raw_top_report':
                month = extract_top_month(filepath)
                if month:
                    snap_date = month_to_snapshot_date(month)
                    df_clean['_snapshot_month'] = month
                    df_data['_snapshot_month'] = month
                    if snap_date:
                        df_clean['_snapshot_date'] = snap_date
                        df_data['_snapshot_date'] = snap_date
                    print(f'  快照月份: {month}')
            elif table_name == 'raw_member_report':
                month = infer_member_snapshot_month(df_data, filepath)
                snap_date = month_to_snapshot_date(month)
                if month:
                    df_clean['_snapshot_month'] = month
                    df_data['_snapshot_month'] = month
                    print(f'  会员快照月份: {month}')
                if snap_date:
                    df_clean['_snapshot_date'] = snap_date
                    df_data['_snapshot_date'] = snap_date

            data_rows = len(df_data)
            removal_parts = []
            if summary_count > 0:
                removal_parts.append(f'删除{summary_count}总计行')
            if zero_count > 0:
                removal_parts.append(f'过滤{zero_count}空数据行')
            removal_desc = '（' + '，'.join(removal_parts) + '）' if removal_parts else ''
            print(f'  原始: {original_rows}行 -> 清洗后: {data_rows}行{removal_desc}')

            stem = os.path.splitext(basename)[0]
            save_cleaned_full(df_clean, os.path.join(cleaned_full_dir, f'{stem}_full.xlsx'))
            df_data.to_csv(os.path.join(cleaned_data_dir, f'{stem}_data.csv'), index=False, encoding='utf-8-sig')

            actual_uploaded = 0
            if data_rows > 0:
                try:
                    actual_uploaded = upload_to_bigquery(df_data, table_name, dry_run=dry_run, source_file=basename)
                    mode = 'dry-run' if dry_run else ''
                    if actual_uploaded == 0 and not dry_run:
                        # 已被 _check_existing_source_file 跳过
                        pass  # 警告已经在 upload_to_bigquery 里印过
                    else:
                        rows_to_show = data_rows if dry_run else (actual_uploaded or data_rows)
                        print(f'  导入 BigQuery: {rows_to_show}行 {mode}✅')
                except Exception as e:
                    err_msg = str(e)
                    print(f'  导入 BigQuery: ❌ {err_msg[:160]}')
                    errors.append((basename, f'BigQuery导入失败: {err_msg[:120]}'))
            else:
                print('  清洗后无数据，跳过导入')

            summary[display_name] = summary.get(display_name, 0) + (actual_uploaded if not dry_run else data_rows)
        except Exception as e:
            print(f'  ❌ 处理失败: {e}')
            errors.append((basename, str(e)[:120]))
            if '--debug' in sys.argv:
                traceback.print_exc()
        print()

    print('=' * 50)
    mode_label = ' (dry-run)' if dry_run else ''
    print(f'  导入完成{mode_label}')
    print('=' * 50)
    total_rows = 0
    for name, count in sorted(summary.items()):
        print(f'  {name}: {count:,}行')
        total_rows += count
    print('  --------')
    print(f'  总计: {total_rows:,}行')
    if errors:
        print(f'\n  ⚠ 有 {len(errors)} 个错误:')
        for fname, err in errors:
            print(f'    - {fname}: {err}')


def clear_all_bigquery_tables():
    try:
        client = bigquery.Client(project=PROJECT_ID)
        tables = list(client.list_tables(f'{PROJECT_ID}.{DATASET_ID}'))
        if not tables:
            print('  BigQuery 中没有表，无需清空。')
            return
        print(f'  找到 {len(tables)} 张表，正在清空...')
        for t in tables:
            client.delete_table(t, not_found_ok=True)
            print(f'  ✅ 已删除: {t.table_id}')
        print('  全部清空完成！导入时会自动重建。')
    except Exception as e:
        print(f'  ❌ 清空失败: {e}')
        print('  请确认已运行 setup.bat 登录 Google 帐号')


def import_bonus_records():
    """导入红利记录（按月份文件夹）"""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '红利记录')
    if not os.path.isdir(base):
        print(f'\n  ❌ 红利记录文件夹不存在: {base}')
        print('  请在运营工具文件夹下建立「红利记录」文件夹，按月份放入子文件夹（如 202601/202602）')
        return

    month_dirs = sorted([d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))])
    if not month_dirs:
        print(f'\n  ❌ 红利记录文件夹为空')
        return

    print(f'\n  检测到 {len(month_dirs)} 个月份文件夹: {", ".join(month_dirs)}')

    frames = []
    for month_dir in month_dirs:
        folder = os.path.join(base, month_dir)
        csv_files = [f for f in os.listdir(folder) if f.endswith('.csv')]
        if not csv_files:
            print(f'  ⚠ {month_dir}: 无CSV文件，跳过')
            continue
        for f in csv_files:
            filepath = os.path.join(folder, f)
            for enc in ('utf-8', 'gb18030', 'gbk', 'big5'):
                try:
                    df = pd.read_csv(filepath, encoding=enc)
                    break
                except Exception:
                    continue
            else:
                print(f'  ⚠ {month_dir}/{f}: 编码识别失败，跳过')
                continue
            df['_source_file'] = f'{month_dir}/{f}'
            frames.append(df)
            print(f'  ✓ {month_dir}/{f}: {len(df)} 行')

    if not frames:
        print('\n  ❌ 没有可导入的数据')
        return

    df = pd.concat(frames, ignore_index=True)

    # 重命名有问题的列名
    if '流水倍数(倍)' in df.columns:
        df = df.rename(columns={'流水倍数(倍)': '流水倍数'})

    # 清洗 ="xxx" 格式
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.replace(r'^="(.*)"$', r'\1', regex=True).str.strip()
            df[col] = df[col].replace({'nan': None, 'None': None, '': None})

    # 活动名称：红利标题为空时取申请备注
    if '红利标题' in df.columns:
        df['活动名称'] = df['红利标题'].fillna('').replace('', None)
        mask = df['活动名称'].isna()
        if '申请备注' in df.columns:
            df.loc[mask, '活动名称'] = df.loc[mask, '申请备注']
        df['活动名称'] = df['活动名称'].fillna('未知')

    # 按申请时间的月份归类
    if '申请时间' in df.columns:
        dt = pd.to_datetime(df['申请时间'], errors='coerce')
        df['_snapshot_month'] = dt.dt.strftime('%Y-%m')

    # 按订单号去重
    before = len(df)
    if '订单号' in df.columns:
        df = df.drop_duplicates(subset=['订单号'], keep='first')
    after = len(df)
    if before != after:
        print(f'\n  去重: {before} → {after}（删除 {before - after} 笔重复）')

    # 数值转换
    for col in ['红利金额', '流水倍数']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    print(f'\n  按月份统计:')
    for m in sorted(df['_snapshot_month'].dropna().unique()):
        mdf = df[df['_snapshot_month'] == m]
        amt = mdf['红利金额'].sum() if '红利金额' in mdf.columns else 0
        print(f'    {m}: {len(mdf)} 笔, 金额 {amt:,.2f}')
    print(f'  总计: {len(df)} 笔')

    # 保存清洗后副本
    clean_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '已清洗')
    os.makedirs(clean_dir, exist_ok=True)
    clean_path = os.path.join(clean_dir, f'红利记录_已清洗_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
    df.to_csv(clean_path, index=False, encoding='utf-8-sig')
    print(f'\n  清洗后文件: {clean_path}')

    # 导入 BigQuery
    print('\n  正在导入 BigQuery...')
    try:
        client = bigquery.Client(project=PROJECT_ID)
        table_id = f'{PROJECT_ID}.{DATASET_ID}.raw_bonus_report'
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            autodetect=True
        )
        job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result()
        print(f'  ✓ 导入完成: {len(df)} 笔 → {table_id}')
    except Exception as e:
        print(f'  ❌ 导入失败: {e}')
        print('  清洗后的文件已保存，可手动导入')


# 客服对话「不满意主因」自动抽取 — 业务关键词规则
# 命中即标,可多标(逗号分隔)
UNHAPPY_ISSUE_CATEGORIES = [
    ('提款问题', ['提款', '取款', '出款', '提现', '到账', '未到', '迟迟', '挂起', '卡住', '审核中', '不出款', '没收到钱']),
    ('充值问题', ['充值', '入款', '存款', '没到帐', '没到账', '充不进', '储值', '入金', '充错', '充值失败']),
    ('红利彩金', ['红利', '彩金', '优惠', '加赠', '没有发', '没发', '没派发', '没派', '没收到红利', '彩金未发', '活动金']),
    ('返水问题', ['返水', '回水', '水钱', '返点', '没有返水', '少给']),
    ('活动规则', ['活动规则', '不符合', '规则', '资格', '条件', '说明不清', '没说', '看不懂规则']),
    ('客服态度', ['态度', '不耐烦', '骂', '凶', '生气', '不专业', '冷淡', '敷衍', '机器人', '回得慢', '不理']),
    ('风控锁定', ['冻结', '锁定', '封号', '禁用', '风控', '审核', '资料审核', '实名', '账号停', '套利']),
    ('账号登录', ['登录', '登入', '密码', '验证码', '账号', '忘记密码', '改密码', '登不进', '账号丢失']),
    ('系统异常', ['系统', '卡', '当机', 'bug', '错误', 'error', '闪退', '加载', '页面', '打不开', '连不上', 'app']),
    ('VIP问题', ['VIP', 'vip', '升级', '降级', '等级', '层级']),
    ('代理佣金', ['代理', '佣金', '团队', '抽成', '分红', '上级']),
    ('游戏问题', ['游戏', '场馆', '注单', '盘口', '体育', '真人', '电子', '彩票', '棋牌', '退款', '走盘']),
    ('链接下载', ['网址', '链接', '下载', '官网', 'APP', 'app', '二维码', 'QR']),
]


def _keyword_match_categories(text: str) -> str:
    """跑关键词字典命中"""
    hits = []
    for category, keywords in UNHAPPY_ISSUE_CATEGORIES:
        for kw in keywords:
            if kw in text:
                hits.append(category)
                break
    return ','.join(hits) if hits else '(未匹配)'


def _strip_canned_prologue(convo: str) -> str:
    """去掉客服对话开头的系统欢迎语 + 客服模板话术,只留实际互动
    包网商每段对话开头都有一段固定模板(平台入款渠道仅能通过官网/温馨提示/极速提款/亿兆专属通讯软件 等),
    会污染关键词匹配,要剥掉
    """
    if not convo:
        return ''
    # 找会员第一次说话的位置 — 通常格式 "会员账号>YYYY-MM-DD ... :"
    import re
    # 先 strip 对话开始 prefix
    m = re.search(r'>(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) :', convo)
    # 收集所有 "xxx>" 段落,过滤掉 "系统>" 跟 客服固定话术
    lines = convo.split('\n')
    keep = []
    skip_canned_phrases = ['平台入款渠道', '官方认证场馆', '极速提款渠道', '亿兆专属通讯软件',
                            '注册后,添加专属客服', '为您带来更流畅的游戏体验', '千里马VPN',
                            '电脑网址', '手机网址', '综合APP', '体育APP', 'fafa']
    for ln in lines:
        if any(p in ln for p in skip_canned_phrases):
            continue
        if ln.strip().startswith('系统>'):
            continue
        keep.append(ln)
    return '\n'.join(keep)


def extract_unhappy_reason(row) -> str:
    """主因抽取 — 优先级: 服务主题 > 评价内容 > 对话内容(去模板后)
    返回:
      - 服务主题 (例: "提款相关 / 提款催促")
      - 或关键词命中类别 (例: "提款问题,客服态度")
      - 或 "(未匹配)"
    """
    # 1) 服务主题最准(系统/客服预先打的标签),直接当主因
    theme = row.get('服务主题')
    if theme is not None and not pd.isna(theme):
        theme_s = str(theme).strip()
        if theme_s and theme_s.lower() not in ('nan', 'none', ''):
            return theme_s

    # 2) 评价内容(客户自己写的差评原因)
    review = row.get('评价内容')
    if review is not None and not pd.isna(review):
        review_s = str(review).strip()
        if review_s and review_s.lower() not in ('nan', 'none', ''):
            return _keyword_match_categories(review_s)

    # 3) 对话内容 (剥掉客服固定话术之后)
    convo = row.get('对话内容')
    if convo is not None and not pd.isna(convo):
        cleaned = _strip_canned_prologue(str(convo))
        if cleaned.strip():
            return _keyword_match_categories(cleaned)

    return '(未匹配)'


def import_cs_conversations():
    """导入客服对话 xlsx（按月份子文件夹）

    资料夹结构:
        dashboard/客服对话/202605/5月客服对话.xlsx  (多 sheets,每个 sheet = 1 天)
        dashboard/客服对话/202606/6月客服对话.xlsx
    一次跑会扫所有月份子资料夹 + 全部 xlsx,WRITE_TRUNCATE 整张 raw_cs_conversations 表
    (跟 raw_bonus_report 同行为 — 简单干净,避免去重逻辑)

    自动衍生栏 `_extracted_issue`:
        只对 满意度评价 ∈ {非常不满意, 不满意} 的行做主因抽取,扫
        「评价内容 + 服务主题 + 对话内容」 用 UNHAPPY_ISSUE_CATEGORIES 关键词规则.
    """
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '客服对话')
    if not os.path.isdir(base):
        print(f'\n  ❌ 客服对话文件夹不存在: {base}')
        print('  请在 dashboard 资料夹下建立「客服对话」资料夹,按月份放入子资料夹(如 202605/202606)')
        return

    month_dirs = sorted([d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))])
    if not month_dirs:
        print('\n  ❌ 客服对话文件夹为空')
        return
    print(f'\n  检测到 {len(month_dirs)} 个月份资料夹: {", ".join(month_dirs)}')

    frames = []
    for month_dir in month_dirs:
        folder = os.path.join(base, month_dir)
        xlsx_files = [f for f in os.listdir(folder) if f.lower().endswith('.xlsx') and not f.startswith('~$')]
        if not xlsx_files:
            print(f'  ⚠ {month_dir}: 无 xlsx 文件,跳过')
            continue
        for fname in xlsx_files:
            fpath = os.path.join(folder, fname)
            try:
                xl = pd.ExcelFile(fpath)
            except Exception as e:
                print(f'  ❌ {month_dir}/{fname}: 读取失败 {e}')
                continue
            sheets = [s for s in xl.sheet_names if s != '总表']
            for sh in sheets:
                try:
                    df = pd.read_excel(fpath, sheet_name=sh)
                except Exception as e:
                    print(f'  ⚠ {month_dir}/{fname}#{sh}: 读取 sheet 失败 {e}')
                    continue
                if df.empty:
                    continue
                df['_sheet'] = sh
                df['_snapshot_month'] = month_dir
                df['_source_file'] = f'{month_dir}/{fname}'
                frames.append(df)
            print(f'  ✓ {month_dir}/{fname}: {len(sheets)} 个 sheet')

    if not frames:
        print('\n  ❌ 没有可导入的数据')
        return

    merged = pd.concat(frames, ignore_index=True)

    # 时间字段
    for c in ['开始时间', '结束时间']:
        if c in merged.columns:
            merged[c] = pd.to_datetime(merged[c], errors='coerce')

    # 数值字段
    for c in ['首次响应', '平均响应', '总时长', '访客消息数', '客服消息数', '撤回消息数', '对话回合数']:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors='coerce')

    # 字符串字段确保 str
    str_cols = ['终端', '访客ID', '对话ID', '新对话ID', '会员账号', '地区', '接待客服',
                '访客IP', '网站名称', '是否邀请评价', '满意度评价', '评价内容',
                '服务主题', '备注', '机器人标识', '对话内容', '_sheet',
                '_snapshot_month', '_source_file']
    for c in str_cols:
        if c in merged.columns:
            merged[c] = merged[c].astype(str).replace({'nan': None, 'None': None, '': None})

    merged['_imported_at'] = pd.Timestamp.now()

    # ★ 不满意主因抽取
    print('\n  扫描「不满意」案件主因...')
    UNHAPPY = {'非常不满意', '不满意'}
    def _extract_row(row):
        sat = str(row.get('满意度评价') or '')
        if sat not in UNHAPPY:
            return ''
        return extract_unhappy_reason(row)
    merged['_extracted_issue'] = [_extract_row(merged.iloc[i]) for i in range(len(merged))]
    unhappy_cnt = (merged['_extracted_issue'] != '').sum()
    print(f'  ✓ 共 {unhappy_cnt} 笔不满意案件已标主因')
    if unhappy_cnt > 0:
        from collections import Counter
        cat_cnt = Counter()
        for s in merged.loc[merged['_extracted_issue'] != '', '_extracted_issue']:
            for cat in s.split(','):
                cat_cnt[cat.strip()] += 1
        print('  主因分布 (top 5):')
        for cat, n in cat_cnt.most_common(5):
            print(f'    {cat}: {n}')

    # 按月份统计
    print(f'\n  合计 {len(merged)} 条对话')
    if '_snapshot_month' in merged.columns:
        for m, sub in merged.groupby('_snapshot_month'):
            print(f'    {m}: {len(sub)} 条')

    # 保存清洗副本
    clean_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '已清洗')
    os.makedirs(clean_dir, exist_ok=True)
    clean_path = os.path.join(clean_dir, f'客服对话_已清洗_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
    merged.to_csv(clean_path, index=False, encoding='utf-8-sig')
    print(f'\n  清洗后文件: {clean_path}')

    # 上传 BQ (WRITE_TRUNCATE 跟 bonus 同行为)
    print('\n  正在导入 BigQuery...')
    try:
        client = bigquery.Client(project=PROJECT_ID)
        table_id = f'{PROJECT_ID}.{DATASET_ID}.raw_cs_conversations'
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            autodetect=True,
        )
        job = client.load_table_from_dataframe(merged, table_id, job_config=job_config)
        job.result()
        print(f'  ✓ 导入完成: {len(merged)} 条 → {table_id}')
    except Exception as e:
        print(f'  ❌ 导入失败: {e}')
        print('  清洗后的文件已保存,可手动导入')


def main():
    parser = argparse.ArgumentParser(description='BigQuery 报表导入工具')
    parser.add_argument('folder', nargs='?', help='报表文件夹路径')
    parser.add_argument('--dry-run', action='store_true', help='仅清洗验证，不导入BigQuery')
    parser.add_argument('--debug', action='store_true', help='显示详细错误信息')
    args = parser.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8', 'utf-8-sig'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    print('  请选择操作：')
    print('  1. 导入运营报表（经营/财务/游戏/会员/代理等）')
    print('  2. 导入红利记录（红利记录文件夹，按月份）')
    print('  3. 仅清洗，不导入（生成比对文件）')
    print('  4. 清空 BigQuery 所有表 + 重新导入')
    print('  5. 仅清空 BigQuery 所有表')
    print('  6. 导入客服对话（客服对话文件夹，按月份）')
    print()
    try:
        choice = input('  请输入数字 (1/2/3/4/5/6): ').strip()
    except (EOFError, KeyboardInterrupt):
        print('\n  已取消')
        return

    if choice == '5':
        clear_all_bigquery_tables()
        return
    if choice == '4':
        clear_all_bigquery_tables()
        choice = '1'

    if choice == '2':
        import_bonus_records()
        return

    if choice == '6':
        import_cs_conversations()
        return

    dry_run = choice == '3'

    folder = args.folder
    if not folder:
        # 默认使用同目录下的「报表资料」文件夹
        default_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), '报表资料')
        if os.path.isdir(default_folder):
            print(f'\n  检测到默认文件夹: {default_folder}')
            use_default = input('  使用此文件夹？(Y/n): ').strip().lower()
            if use_default != 'n':
                folder = default_folder
        if not folder:
            print('\n  请输入报表文件夹路径（或直接拖入文件夹）:')
            try:
                folder = input('  > ').strip().strip('"').strip("'")
            except (EOFError, KeyboardInterrupt):
                print('\n  已取消')
                return
    if not folder or not os.path.isdir(folder):
        print(f'\n  ❌ 文件夹不存在: {folder}')
        return
    process_folder(folder, dry_run=dry_run)


if __name__ == '__main__':
    main()
