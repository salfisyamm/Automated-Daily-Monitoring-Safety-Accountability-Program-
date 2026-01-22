import zipfile
from pathlib import Path
from datetime import datetime
import shutil
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import gspread
from google.oauth2.service_account import Credentials
from matplotlib.colors import to_rgba

# === imports untuk render PNG ===
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Share Fonte
import requests
import os
import time


# =========================================================
# PATH
# =========================================================
RAW_DIR = Path(r"D:\Project\automation daily job\data\raw")
PROCESSED_DIR = Path(r"D:\Project\automation daily job\data\processed")

RUN_DATE = datetime.now().strftime("%Y-%m-%d")
EXTRACT_DIR = PROCESSED_DIR / "extracted" / RUN_DATE
EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

BATCH_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

# =========================================================
# POSTGRES
# =========================================================
PG = dict(
    host="localhost",
    port=5432,
    dbname="SAP",
    user="postgres",
    password="060c",
)

STG_SCHEMA = '1.STG_SAP'
FACT_SCHEMA = '3.FACT_SAP'
FINAL_SCHEMA = '4.FINAL_SAP'

# =========================================================
# GOOGLE SHEETS (CREDENTIAL) — dipakai untuk ABSENSI import
# =========================================================
GSHEET_CRED_PATH = r"D:\Project\automation daily job\config\credentials gsheet.json"

# =========================================================
# TABLE MAP
# =========================================================
TABLE_MAP = {
    "car": "stg_car",
    "observasi": "stg_observasi",
    "coaching": "stg_coaching",
    "oak": "stg_oak",
}

FACT_TABLE_MAP = {
    "car": "fact_car",
    "observasi": "fact_observasi",
    "coaching": "fact_coaching",
    "oak": "fact_oak",
}

PK_COL = {
    "car": "#Task",
    "observasi": "No ID",
    "coaching": "No ID",
    "oak": "No Id",
}

# =========================================================
# ENRICHMENT CONFIG (FINAL & AMAN)
# =========================================================
ENRICHMENT_MAP = {
    "car": {
        "created_parts": ["Tahun", "Bulan", "Tanggal", "Jam", "Menit"],
        "enable_gap": True,
        "status_col": "Status",
        "due_text_col": "Due Date Penyelesaian",
    },
    "observasi": {
        "created_parts": ["Tahun", "Bulan", "Tanggal", "Jam", "Menit"],
        "enable_gap": False,
    },
    "coaching": {
        "created_parts": ["Tahun", "Bulan", "Tanggal", "Jam", "Menit"],
        "enable_gap": False,
    },
    "oak": {
        "created_parts": ["Tahun", "Bulan", "Tanggal", "Jam", "Menit"],
        "enable_gap": False,
    },
}

# =========================================================
# HELPERS
# =========================================================
def guess_dataset_from_name(name: str) -> str:
    s = name.lower()
    if "oak" in s:
        return "oak"
    if "coaching" in s:
        return "coaching"
    if "observasi" in s or "observation" in s:
        return "observasi"
    return "car"


def read_csv_robust(path: Path) -> pd.DataFrame:
    for sep in [",", ";", "\t"]:
        for enc in ["utf-8", "utf-8-sig", "cp1252", "latin1"]:
            try:
                df = pd.read_csv(path, sep=sep, encoding=enc)
                if df.shape[1] > 1:
                    return df
            except Exception:
                pass
    return pd.read_csv(path, engine="python")


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
    return df


def normalize_pk_for_car(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "#Task" not in df.columns and "Task" in df.columns:
        df = df.rename(columns={"Task": "#Task"})
    return df


def filter_rows(dataset: str, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if dataset == "coaching" and "No ID" in df.columns:
        s = df["No ID"]
        df = df[s.notna() & (s.astype(str).str.strip() != "")]
    if dataset == "oak" and "Tipe Observer" in df.columns:
        df = df[df["Tipe Observer"].astype(str).str.upper() == "OBSERVER"]
    return df


def clean_pk_series(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce").astype("Int64").astype(str)
    s = s.astype(str).str.strip()
    return s.str.replace(r"\.0$", "", regex=True).replace({"": None, "nan": None})


# Ambil Absen dari Link Gsheet
def get_last_visible_worksheet(sh) -> gspread.Worksheet:
    wss = sh.worksheets()  # urutan kiri → kanan
    visible = []
    for ws in wss:
        hidden = bool(getattr(ws, "_properties", {}).get("hidden", False))
        if not hidden:
            visible.append(ws)
    return visible[-1] if visible else wss[-1]


# Set Week
def get_week_config(conn):
    sql = '''
        SELECT week_key, week_start_date, window_start_ts, window_end_ts, updated_at
        FROM "2.DIM_SAP".config_monitoring_week
        WHERE id = 1
    '''
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()

    if not row:
        raise RuntimeError('config_monitoring_week belum ada. Jalankan SQL create table + insert id=1.')

    return dict(
        week_key=row[0],
        week_start_date=row[1],
        window_start_ts=row[2],
        window_end_ts=row[3],
        updated_at=row[4],
    )


DAY_ID = {
    0: "SENIN",
    1: "SELASA",
    2: "RABU",
    3: "KAMIS",
    4: "JUMAT",
    5: "SABTU",
    6: "MINGGU",
}

MONTH_ID = {
    1: "JANUARI",  2: "FEBRUARI", 3: "MARET",
    4: "APRIL",    5: "MEI",      6: "JUNI",
    7: "JULI",     8: "AGUSTUS",  9: "SEPTEMBER",
    10: "OKTOBER", 11: "NOVEMBER", 12: "DESEMBER",
}

def format_update_id(dt: datetime) -> str:
    hari = DAY_ID[dt.weekday()]
    bulan = MONTH_ID[dt.month]
    return f"{hari}, {dt:%d} {bulan} {dt:%Y} {dt:%H:%M}"

def make_title_png(dept_name: str, dt: datetime) -> str:
    # untuk PNG (JANGAN pakai * karena nanti bintang ikut ke gambar)
    return f"SAP {str(dept_name).upper()}\nUPDATE : {format_update_id(dt)}"

def make_caption_wa_bold(dept_name: str, dt: datetime) -> str:
    # untuk WhatsApp (bold)
    return f"*SAP {str(dept_name).upper()}*\n*UPDATE : {format_update_id(dt)}*"


# =========================================================
# STG UPSERT
# =========================================================
def upsert_df_to_stg(conn, dataset: str, df: pd.DataFrame, source_file: str):
    if df.empty:
        return

    stg = TABLE_MAP[dataset]
    pk = PK_COL[dataset]
    df = df.copy()
    df[pk] = clean_pk_series(df[pk])
    df = df[df[pk].notna()].drop_duplicates(subset=[pk], keep="last")
    df = df.where(pd.notnull(df), None)

    cols = list(df.columns)
    full_table = f'"{STG_SCHEMA}".{stg}'

    insert_cols = ['batch_id', 'source_file', 'loaded_at'] + cols
    insert_q = ", ".join([f'"{c}"' for c in insert_cols])
    set_clause = ", ".join([f'"{c}"=EXCLUDED."{c}"' for c in insert_cols if c != pk])

    sql = f"""
        INSERT INTO {full_table} ({insert_q})
        VALUES %s
        ON CONFLICT ("{pk}") DO UPDATE SET {set_clause};
    """

    rows = [(BATCH_ID, source_file, datetime.now(), *[r[c] for c in cols]) for _, r in df.iterrows()]
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)


# =========================================================
# FACT UPSERT
# =========================================================
def upsert_fact(conn, dataset: str):
    stg = TABLE_MAP[dataset]
    fact = FACT_TABLE_MAP[dataset]
    pk = PK_COL[dataset]

    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
        """, (STG_SCHEMA, stg))
        cols = [r[0] for r in cur.fetchall()]

    meta = {"batch_id", "source_file", "loaded_at"}
    data_cols = [c for c in cols if c not in meta]

    qcols = ", ".join([f'"{c}"' for c in data_cols])
    set_clause = ", ".join([f'"{c}"=EXCLUDED."{c}"' for c in data_cols if c != pk])

    sql = f"""
        INSERT INTO "{FACT_SCHEMA}".{fact} ({qcols})
        SELECT {qcols}
        FROM "{STG_SCHEMA}".{stg}
        WHERE batch_id=%s
        ON CONFLICT ("{pk}") DO UPDATE SET {set_clause};
    """

    with conn.cursor() as cur:
        cur.execute(sql, (BATCH_ID,))


# =========================================================
# FACT ENRICHMENT
# =========================================================
def ensure_fact_columns(conn, dataset: str):
    fact = FACT_TABLE_MAP[dataset]
    full = f'"{FACT_SCHEMA}".{fact}'

    cols = {"new_week": "text"}

    # khusus CAR: flag TBC berdasarkan DIM sub_ketidaksesuaian
    if dataset == "car":
        cols["tbc"] = "boolean"

    if ENRICHMENT_MAP[dataset].get("enable_gap"):
        cols["gap_submit_open"] = "interval"
        cols["gap_progress"] = "interval"

    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
        """, (FACT_SCHEMA, fact))
        existing = {r[0] for r in cur.fetchall()}

        for c, t in cols.items():
            if c not in existing:
                cur.execute(f'ALTER TABLE {full} ADD COLUMN "{c}" {t};')


def refresh_fact_enrichment(conn, dataset: str):
    ensure_fact_columns(conn, dataset)
    cfg = ENRICHMENT_MAP[dataset]
    fact = FACT_TABLE_MAP[dataset]
    full = f'"{FACT_SCHEMA}".{fact}'

    y, m, d, h, mi = cfg["created_parts"]
    created_ts = f'make_timestamp("{y}","{m}","{d}","{h}","{mi}",0)'
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# date_trunc('week', ({created_ts} - interval '6 hours')) + interval '6 hours',
# Untuk Ganti Jadi Cutoff jam 06.00
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    sql_new_week = f"""
        UPDATE {full}
        SET new_week = to_char(
            date_trunc('week', {created_ts}),
            'IYYY-"W"IW'
        )
        WHERE new_week IS NULL;
    """

    with conn.cursor() as cur:
        # 1) set new_week
        cur.execute(sql_new_week)

        # 2) khusus CAR: update flag tbc berdasar DIM
        if dataset == "car":
            cur.execute(f"""
                UPDATE {full} fc
                SET tbc = EXISTS (
                  SELECT 1
                  FROM "2.DIM_SAP".dim_validasi_tbc_subketidaksesuaian d
                  WHERE upper(trim(fc."Sub Ketidaksesuaian")) = upper(trim(d.sub_ketidaksesuaian))
                );
            """)

        # 3) gap columns
        if cfg.get("enable_gap"):
            status = cfg["status_col"]
            due = cfg["due_text_col"]

            cur.execute(f"""
                UPDATE {full}
                SET gap_submit_open =
                    CASE
                        WHEN UPPER(TRIM("{status}")) IN ('OPEN','SUBMITTED')
                        THEN now() - {created_ts}
                        ELSE NULL
                    END;
            """)

            cur.execute(f"""
                UPDATE {full}
                SET gap_progress =
                    CASE
                        WHEN UPPER(TRIM("{status}"))='PROGRESS'
                         AND "{due}" IS NOT NULL
                         AND TRIM("{due}") <> ''
                        THEN to_date("{due}",'DD-Mon-YYYY')::timestamp - now()
                        ELSE NULL
                    END;
            """)

    print(f"[ENRICH:{dataset}] OK")


# =========================================================
# FINAL VIEWS + overdue_monitoring
# =========================================================
def create_final_views(conn):
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{FINAL_SCHEMA}";')

        # =========================================================
        # overdue_monitoring
        # =========================================================
        cur.execute(f'DROP VIEW IF EXISTS "{FINAL_SCHEMA}".overdue_monitoring CASCADE;')
        cur.execute(f"""
            CREATE VIEW "{FINAL_SCHEMA}".overdue_monitoring AS
            SELECT
              "#Task"                 AS "Task",
              "Status",
              "PIC",
              "Tanggal Pembuatan",
              "Due Date Penyelesaian",
              gap_progress            AS "GAP Closing",
              "Pelapor",
              "Perusahaan Pelapor",
              "Deskripsi"             AS "Deskripsi Laporan",
              "Foto Laporan"          AS "Foto Temuan"
            FROM "{FACT_SCHEMA}".fact_car
            WHERE
              TRIM("Perusahaan PIC") = 'PT Madhani Talatah Nusantara'
              AND (
                (
                  UPPER(TRIM("Status")) = 'PROGRESS'
                  AND gap_progress IS NOT NULL
                  AND gap_progress < interval '20 days'
                )
                OR
                UPPER(TRIM("Status")) = 'OVERDUE'
              )
            ORDER BY gap_progress ASC NULLS LAST;
        """)

        # =========================================================
        # monitoring_sap (SUDAH include Post Event + Real Time)
        # =========================================================
        cur.execute(f'DROP VIEW IF EXISTS "{FINAL_SCHEMA}".monitoring_sap CASCADE;')
        cur.execute(f"""
            CREATE VIEW "{FINAL_SCHEMA}".monitoring_sap AS
            WITH
            cfg AS (
              SELECT week_key, week_start_date
              FROM "2.DIM_SAP".config_monitoring_week
              WHERE id = 1
            ),

            abs_base AS (
              SELECT
                a.week_start_date,
                cfg.week_key AS week_key,
                a.blok,
                TRIM(BOTH FROM a.nik) AS nik,
                a.nama,
                a.departemen AS management,
                a.layer AS layering,
                a.jabatan AS pja,
                a.present_days
              FROM "{FACT_SCHEMA}".absensi_weekly_raw a
              CROSS JOIN cfg
              WHERE a.week_start_date = cfg.week_start_date
            ),

            abs_enriched AS (
              SELECT
                b.week_start_date,
                b.week_key,
                b.blok,
                b.nik,
                b.nama,
                b.management,
                b.layering,
                b.pja,
                b.present_days,

                NULLIF(regexp_replace(b.layering, E'\\\\D', '', 'g'), '')::integer AS layer_num,

                (
                  NULLIF(regexp_replace(b.layering, E'\\\\D', '', 'g'), '')::integer = 1
                  AND upper(COALESCE(b.management, '')) ~~ '%OPERATION%'
                )
                OR
                (
                  NULLIF(regexp_replace(b.layering, E'\\\\D', '', 'g'), '')::integer = 3
                  AND upper(COALESCE(b.management, '')) ~~ '%HSE%'
                  AND (b.nik = ANY (ARRAY['17365','16526','16692F','17891']))
                ) AS is_special_7

              FROM abs_base b
              WHERE NULLIF(regexp_replace(b.layering, E'\\\\D', '', 'g'), '')::integer BETWEEN 1 AND 4
            ),

            car_actual AS (
              SELECT
                fc.new_week AS week_key,
                TRIM(BOTH FROM fc."NPK Pelapor") AS nik,
                SUM(CASE WHEN upper(TRIM(BOTH FROM fc."Sumber Data")) = 'INSPEKSI' THEN 1 ELSE 0 END)::integer AS inspeksi_aktual,
                SUM(CASE WHEN upper(TRIM(BOTH FROM fc."Sumber Data")) = 'HAZARD'   THEN 1 ELSE 0 END)::integer AS hazard_aktual,
                SUM(CASE WHEN COALESCE(fc.tbc,false) THEN 1 ELSE 0 END)::integer AS tbc_aktual
              FROM "{FACT_SCHEMA}".fact_car fc
              CROSS JOIN cfg
              WHERE fc.new_week = cfg.week_key
              GROUP BY fc.new_week, TRIM(BOTH FROM fc."NPK Pelapor")
            ),

            obs_actual AS (
              SELECT
                fo.new_week AS week_key,
                TRIM(BOTH FROM fo."NPK Pelapor") AS nik,
                COUNT(*)::integer AS observasi_aktual
              FROM "{FACT_SCHEMA}".fact_observasi fo
              CROSS JOIN cfg
              WHERE fo.new_week = cfg.week_key
              GROUP BY fo.new_week, TRIM(BOTH FROM fo."NPK Pelapor")
            ),

            coach_actual AS (
              SELECT
                fk.new_week AS week_key,
                TRIM(BOTH FROM fk."NPK Coach") AS nik,
                COUNT(*)::integer AS coaching_aktual
              FROM "{FACT_SCHEMA}".fact_coaching fk
              CROSS JOIN cfg
              WHERE fk.new_week = cfg.week_key
              GROUP BY fk.new_week, TRIM(BOTH FROM fk."NPK Coach")
            ),

            oak_actual AS (
              SELECT
                foa.new_week AS week_key,
                TRIM(BOTH FROM foa."NPK Karyawan Observer") AS nik,
                COUNT(*)::integer AS oak_aktual
              FROM "{FACT_SCHEMA}".fact_oak foa
              CROSS JOIN cfg
              WHERE foa.new_week = cfg.week_key
              GROUP BY foa.new_week, TRIM(BOTH FROM foa."NPK Karyawan Observer")
            ),

            -- NEW: union Jenis TP dari 3 fact
            tp_union AS (
              SELECT
                fc.new_week AS week_key,
                TRIM(BOTH FROM fc."NPK Pelapor") AS nik,
                upper(trim(fc."Jenis TP")) AS jenis_tp
              FROM "{FACT_SCHEMA}".fact_car fc
              CROSS JOIN cfg
              WHERE fc.new_week = cfg.week_key

              UNION ALL

              SELECT
                fo.new_week AS week_key,
                TRIM(BOTH FROM fo."NPK Pelapor") AS nik,
                upper(trim(fo."Jenis TP")) AS jenis_tp
              FROM "{FACT_SCHEMA}".fact_observasi fo
              CROSS JOIN cfg
              WHERE fo.new_week = cfg.week_key

              UNION ALL

              SELECT
                foa.new_week AS week_key,
                TRIM(BOTH FROM foa."NPK Karyawan Observer") AS nik,
                upper(trim(foa."Jenis TP")) AS jenis_tp
              FROM "{FACT_SCHEMA}".fact_oak foa
              CROSS JOIN cfg
              WHERE foa.new_week = cfg.week_key
            ),

            tp_actual AS (
              SELECT
                week_key,
                nik,
                SUM(CASE WHEN jenis_tp = 'POST EVENT' THEN 1 ELSE 0 END)::integer AS post_event_aktual,
                SUM(CASE WHEN jenis_tp IN ('REAL TIME','REALTIME') THEN 1 ELSE 0 END)::integer AS real_time_aktual
              FROM tp_union
              WHERE nik IS NOT NULL AND trim(nik) <> ''
              GROUP BY week_key, nik
            ),

            base AS (
              SELECT
                a.week_key,
                a.week_start_date,
                a.blok,
                a.nik,
                a.nama,
                a.management,
                a.layering,
                a.pja,
                a.present_days,
                a.layer_num,
                a.is_special_7,

                COALESCE(c.inspeksi_aktual, 0) AS inspeksi_aktual,
                COALESCE(c.hazard_aktual, 0)   AS hazard_aktual,
                COALESCE(c.tbc_aktual, 0)      AS tbc_aktual,

                COALESCE(o.observasi_aktual, 0) AS observasi_aktual,
                COALESCE(k.coaching_aktual, 0)  AS coaching_aktual,
                COALESCE(oa.oak_aktual, 0)      AS oak_aktual,

                -- NEW
                COALESCE(tp.post_event_aktual, 0) AS post_event_aktual,
                COALESCE(tp.real_time_aktual, 0)  AS real_time_aktual

              FROM abs_enriched a
              LEFT JOIN car_actual   c  ON c.week_key  = a.week_key AND c.nik  = a.nik
              LEFT JOIN obs_actual   o  ON o.week_key  = a.week_key AND o.nik  = a.nik
              LEFT JOIN coach_actual k  ON k.week_key  = a.week_key AND k.nik  = a.nik
              LEFT JOIN oak_actual   oa ON oa.week_key = a.week_key AND oa.nik = a.nik
              LEFT JOIN tp_actual    tp ON tp.week_key = a.week_key AND tp.nik = a.nik
            ),

            plan_calc AS (
              SELECT
                b.*,

                CASE
                  WHEN b.present_days <= 4 THEN 0
                  WHEN b.is_special_7 = false THEN 1
                  ELSE
                    CASE
                      WHEN (b.inspeksi_aktual + b.hazard_aktual) > 0
                        THEN round(b.present_days::numeric * b.inspeksi_aktual::numeric / (b.inspeksi_aktual + b.hazard_aktual)::numeric)::integer
                      ELSE round(b.present_days::numeric * 3::numeric / 7::numeric)::integer
                    END
                END AS inspeksi_plan,

                CASE
                  WHEN b.present_days <= 4 THEN 0
                  WHEN b.is_special_7 = false THEN 1
                  ELSE b.present_days -
                    CASE
                      WHEN (b.inspeksi_aktual + b.hazard_aktual) > 0
                        THEN round(b.present_days::numeric * b.inspeksi_aktual::numeric / (b.inspeksi_aktual + b.hazard_aktual)::numeric)::integer
                      ELSE round(b.present_days::numeric * 3::numeric / 7::numeric)::integer
                    END
                END AS hazard_plan,

                CASE
                  WHEN b.present_days <= 4 THEN 0
                  WHEN b.layer_num = 1 THEN b.present_days * 3
                  WHEN b.layer_num = 2 THEN b.present_days * 2
                  WHEN b.layer_num = ANY (ARRAY[3,4]) THEN b.present_days * 1
                  ELSE 0
                END AS oak_plan,

                CASE
                  WHEN b.present_days <= 4 THEN 0
                  WHEN b.layer_num = 1 THEN b.present_days * 1
                  WHEN b.layer_num = 2 THEN 3
                  WHEN b.layer_num = ANY (ARRAY[3,4]) THEN 2
                  ELSE 0
                END AS observasi_plan,

                CASE
                  WHEN b.present_days <= 4 THEN 0
                  ELSE 3
                END AS coaching_plan

              FROM base b
            ),

            -- NEW: plan Post Event & Real Time
            plan_tp AS (
              SELECT
                p.*,

                CASE
                  WHEN p.present_days <= 4 THEN 0
                  WHEN p.layer_num = 1 THEN 0
                  WHEN p.layer_num = 2 THEN 7
                  WHEN p.layer_num = 3 THEN 3
                  WHEN p.layer_num = 4 THEN 1
                  ELSE 0
                END AS post_event_plan,

                CASE
                  WHEN p.present_days <= 4 THEN 0
                  WHEN p.layer_num = 1 THEN p.oak_plan
                  WHEN p.layer_num = 2 THEN 7
                  WHEN p.layer_num = 3 THEN 3
                  WHEN p.layer_num = 4 THEN 1
                  ELSE 0
                END AS real_time_plan

              FROM plan_calc p
            ),

            final AS (
              SELECT
                p.week_key,
                p.week_start_date,
                p.blok,
                p.nik,
                p.nama,
                p.management,
                p.layering,
                p.pja,

                p.inspeksi_plan,
                p.inspeksi_aktual,
                CASE WHEN p.inspeksi_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.inspeksi_aktual::numeric/p.inspeksi_plan::numeric)::integer)
                END AS inspeksi_ach,

                p.hazard_plan,
                p.hazard_aktual,
                CASE WHEN p.hazard_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.hazard_aktual::numeric/p.hazard_plan::numeric)::integer)
                END AS hazard_ach,

                (p.inspeksi_plan + p.hazard_plan) AS tbc_plan,
                p.tbc_aktual,
                CASE WHEN (p.inspeksi_plan+p.hazard_plan)=0 THEN 0
                     ELSE LEAST(100, round(100.0*p.tbc_aktual::numeric/(p.inspeksi_plan+p.hazard_plan)::numeric)::integer)
                END AS tbc_ach,

                p.oak_plan,
                p.oak_aktual,
                CASE WHEN p.oak_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.oak_aktual::numeric/p.oak_plan::numeric)::integer)
                END AS oak_ach,

                p.observasi_plan,
                p.observasi_aktual,
                CASE WHEN p.observasi_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.observasi_aktual::numeric/p.observasi_plan::numeric)::integer)
                END AS observasi_ach,

                p.coaching_plan,
                p.coaching_aktual,
                CASE WHEN p.coaching_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.coaching_aktual::numeric/p.coaching_plan::numeric)::integer)
                END AS coaching_ach,

                -- NEW: Post Event
                p.post_event_plan,
                p.post_event_aktual,
                CASE WHEN p.post_event_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.post_event_aktual::numeric/p.post_event_plan::numeric)::integer)
                END AS post_event_ach,

                -- NEW: Real Time
                p.real_time_plan,
                p.real_time_aktual,
                CASE WHEN p.real_time_plan = 0 THEN 0
                     ELSE LEAST(100, round(100.0*p.real_time_aktual::numeric/p.real_time_plan::numeric)::integer)
                END AS real_time_ach,

                -- ✅ Ach Total = rata-rata 5 item (tanpa TBC)
                round(
                  (
                    CASE WHEN p.inspeksi_plan  > 0 THEN LEAST(100, round(100.0*p.inspeksi_aktual::numeric/p.inspeksi_plan::numeric)::integer) ELSE 0 END +
                    CASE WHEN p.hazard_plan    > 0 THEN LEAST(100, round(100.0*p.hazard_aktual::numeric/p.hazard_plan::numeric)::integer) ELSE 0 END +
                    CASE WHEN p.oak_plan       > 0 THEN LEAST(100, round(100.0*p.oak_aktual::numeric/p.oak_plan::numeric)::integer) ELSE 0 END +
                    CASE WHEN p.observasi_plan > 0 THEN LEAST(100, round(100.0*p.observasi_aktual::numeric/p.observasi_plan::numeric)::integer) ELSE 0 END +
                    CASE WHEN p.coaching_plan  > 0 THEN LEAST(100, round(100.0*p.coaching_aktual::numeric/p.coaching_plan::numeric)::integer) ELSE 0 END
                  )::numeric
                  /
                  NULLIF(
                    (CASE WHEN p.inspeksi_plan  > 0 THEN 1 ELSE 0 END +
                     CASE WHEN p.hazard_plan    > 0 THEN 1 ELSE 0 END +
                     CASE WHEN p.oak_plan       > 0 THEN 1 ELSE 0 END +
                     CASE WHEN p.observasi_plan > 0 THEN 1 ELSE 0 END +
                     CASE WHEN p.coaching_plan  > 0 THEN 1 ELSE 0 END),
                    0
                  )
                )::integer AS ach_total

              FROM plan_tp p
            )

            SELECT
              nik AS "NIK",
              nama AS "NAMA",
              management AS "DEPARTEMEN",
              layering AS "LAYERING",
              pja AS "PJA",
              ach_total AS "Ach. Total",
              inspeksi_plan AS "Inspeksi Plan",
              inspeksi_aktual AS "Inspeksi Aktual",
              inspeksi_ach AS "Inspeksi Ach",
              hazard_plan AS "Hazard Plan",
              hazard_aktual AS "Hazard Aktual",
              hazard_ach AS "Hazard Ach",
              tbc_plan AS "TBC Plan",
              tbc_aktual AS "TBC Aktual",
              tbc_ach AS "TBC Ach",
              oak_plan AS "OAK Plan",
              oak_aktual AS "OAK Aktual",
              oak_ach AS "OAK Ach",
              observasi_plan AS "Observasi Plan",
              observasi_aktual AS "Observasi Aktual",
              observasi_ach AS "Observasi Ach",
              coaching_plan AS "Coaching Plan",
              coaching_aktual AS "Coaching Aktual",
              coaching_ach AS "Coaching Ach",
              week_key AS "Week",
              blok AS "Blok",

              -- ✅ kolom baru DI AKHIR (aman untuk view)
              post_event_plan   AS "Post Event Plan",
              post_event_aktual AS "Post Event Aktual",
              post_event_ach    AS "Post Event Ach",
              real_time_plan    AS "Real Time Plan",
              real_time_aktual  AS "Real Time Aktual",
              real_time_ach     AS "Real Time Ach"
            FROM final;
        """)

    print('[FINAL] views overdue_monitoring + monitoring_sap created')

# =========================================================
# GOOGLE SHEETS IMPORT VALIDASI ABSEN
# =========================================================
ABSENSI_SHEETS = [
    {"blok": "UTARA",   "spreadsheet_id": "1Aj3jKuh72sAtPXExp8UOcefqjcU2hcN7XmFIPVoa_No"},
    {"blok": "SELATAN", "spreadsheet_id": "19ajvwd3xShVHlCNWaGL-KbZd5UKWqO148Ds4TUbMBG0"},
]

def upsert_absensi_from_gsheets(conn):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GSHEET_CRED_PATH, scopes=scopes)
    gc = gspread.authorize(creds)

    today = pd.Timestamp(datetime.now())
    week_start_date = (today - pd.Timedelta(days=today.weekday())).date()

    full_table = f'"{FACT_SCHEMA}".absensi_weekly_raw'

    for cfg in ABSENSI_SHEETS:
        blok = cfg["blok"]
        sh = gc.open_by_key(cfg["spreadsheet_id"])

        ws = get_last_visible_worksheet(sh)   # ⬅️ PALING KANAN
        tab_name = ws.title

        values = ws.get("A1:Q")
        values = values[:1000]

        if not values or len(values) < 2:
            print(f"[ABSENSI:{blok}] EMPTY tab={tab_name}")
            continue

        # DETECT HEADER ROW (cari baris yang ada "NIK")
        header_row_idx = None
        for i, row in enumerate(values):
            row_upper = [str(c).strip().upper() for c in row]
            if "NIK" in row_upper:
                header_row_idx = i
                break

        if header_row_idx is None:
            raise ValueError(f"[ABSENSI:{blok}] Header row with 'NIK' not found in tab {tab_name}")

        header = [str(h).strip().upper() for h in values[header_row_idx]]
        data_rows = values[header_row_idx + 1:]
        df = pd.DataFrame(data_rows, columns=header)

        required = [
            "NIK","SID","NAMA","JABATAN","DEPARTEMEN","LAYER",
            "SENIN","SELASA","RABU","KAMIS","JUMAT","SABTU","MINGGU"
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"[ABSENSI:{blok}] Missing columns: {missing}")

        df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)
        df = df.replace({"": None, "nan": None, "None": None})

        day_cols = ["SENIN","SELASA","RABU","KAMIS","JUMAT","SABTU","MINGGU"]
        df["absent_days"] = df[day_cols].notna().sum(axis=1).astype(int)
        df["present_days"] = (7 - df["absent_days"]).astype(int)

        df["eligible_target"] = df["absent_days"].eq(0)
        df["target_reduction"] = df["absent_days"].astype(float)

        df["NIK"] = df["NIK"].astype(str).str.strip()
        df = df[df["NIK"].notna() & (df["NIK"] != "")].drop_duplicates(subset=["NIK"], keep="last")

        sql = f"""
            INSERT INTO {full_table} (
              week_start_date, blok, nik, sid, nama, jabatan, departemen, layer,
              senin, selasa, rabu, kamis, jumat, sabtu, minggu,
              absent_days, present_days, eligible_target, target_reduction,
              source_tab, updated_at
            )
            VALUES %s
            ON CONFLICT (week_start_date, blok, nik) DO UPDATE SET
              sid=EXCLUDED.sid,
              nama=EXCLUDED.nama,
              jabatan=EXCLUDED.jabatan,
              departemen=EXCLUDED.departemen,
              layer=EXCLUDED.layer,
              senin=EXCLUDED.senin,
              selasa=EXCLUDED.selasa,
              rabu=EXCLUDED.rabu,
              kamis=EXCLUDED.kamis,
              jumat=EXCLUDED.jumat,
              sabtu=EXCLUDED.sabtu,
              minggu=EXCLUDED.minggu,
              absent_days=EXCLUDED.absent_days,
              present_days=EXCLUDED.present_days,
              eligible_target=EXCLUDED.eligible_target,
              target_reduction=EXCLUDED.target_reduction,
              source_tab=EXCLUDED.source_tab,
              updated_at=EXCLUDED.updated_at;
        """

        now_ts = datetime.now()
        rows = []
        for _, r in df.iterrows():
            rows.append((
                week_start_date, blok,
                r["NIK"], r["SID"], r["NAMA"], r["JABATAN"],
                r["DEPARTEMEN"], r["LAYER"],
                r["SENIN"], r["SELASA"], r["RABU"],
                r["KAMIS"], r["JUMAT"], r["SABTU"], r["MINGGU"],
                int(r["absent_days"]), int(r["present_days"]),
                bool(r["eligible_target"]), float(r["target_reduction"]),
                tab_name, now_ts
            ))

        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=1000)

        print(f"[ABSENSI:{blok}] OK rows={len(rows)} tab={tab_name}")


# =========================================================
# RENDER PNG (AMBIL DARI FINAL VIEW)
# =========================================================
FINAL_VIEW = '"4.FINAL_SAP".monitoring_sap'

OUT_DIR = Path(r"D:\Project\automation daily job\output\png")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SQL_SELECT = f"""
SELECT
  "NIK"                AS "NIK",
  "NAMA"               AS "NAMA",
  "DEPARTEMEN"         AS "DEPARTEMEN",
  "LAYERING"           AS "LAYER",

  "Ach. Total"         AS "Ach Total",

  "Inspeksi Plan"      AS "Ins Plan",
  "Inspeksi Aktual"    AS "Ins Aktual",
  "Inspeksi Ach"       AS "Ins Ach",

  "Hazard Plan"        AS "Hz Plan",
  "Hazard Aktual"      AS "Hz Aktual",
  "Hazard Ach"         AS "Hz Ach",

  "TBC Plan"           AS "TBC Plan",
  "TBC Aktual"         AS "TBC Aktual",
  "TBC Ach"            AS "TBC Ach",

  "OAK Plan"           AS "OAK Plan",
  "OAK Aktual"         AS "OAK Aktual",
  "OAK Ach"            AS "OAK Ach",

  "Observasi Plan"     AS "Obs Plan",
  "Observasi Aktual"   AS "Obs Aktual",
  "Observasi Ach"      AS "Obs Ach",

  "Coaching Plan"      AS "Coach Plan",
  "Coaching Aktual"    AS "Coach Aktual",
  "Coaching Ach"       AS "Coach Ach",

  -- NEW
  "Post Event Plan"    AS "PE Plan",
  "Post Event Aktual"  AS "PE Aktual",
  "Post Event Ach"     AS "PE Ach",

  "Real Time Plan"     AS "RT Plan",
  "Real Time Aktual"   AS "RT Aktual",
  "Real Time Ach"      AS "RT Ach"

FROM {FINAL_VIEW}
WHERE COALESCE("Inspeksi Plan", 0) <> 0
ORDER BY
  NULLIF(regexp_replace("LAYERING", '\\D', '', 'g'), '')::int ASC,
  "Ach. Total" DESC NULLS LAST,
  "NAMA"
"""

def to_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, str):
            s = x.strip().replace("%", "")
            if s == "":
                return None
            return float(s)
        return float(x)
    except Exception:
        return None

def ach_color(val):
    v = to_float(val)
    if v is None:
        return None
    if v >= 100:
        return "#4CAF50"  # hijau
    if v >= 75:
        return "#FFC107"  # kuning
    return "#F44336"      # merah

def render_png(df: pd.DataFrame, title: str, out_path: Path, max_rows: int = 30):
    if max_rows:
        df = df.head(max_rows)

    df = df.copy().fillna("")
    nrows, ncols = df.shape

    # =========================================
    # FORMAT: semua kolom Ach jadi "xx%"
    # =========================================
    def fmt_pct(v):
        vv = to_float(v)
        if vv is None:
            return ""
        if float(vv).is_integer():
            return f"{int(vv)}%"
        return f"{vv:.1f}%"

    ach_cols = [c for c in df.columns if str(c).lower().endswith("ach") or "ach total" in str(c).lower()]
    for c in ach_cols:
        df[c] = df[c].apply(fmt_pct)

    COLS = list(df.columns)
    LEFT_COLS = ["NIK", "NAMA", "DEPARTEMEN", "LAYER", "Ach Total"]

    groups = [
        ("Inspeksi",   COLS.index("Ins Plan"),   3, "#143F6B"),
        ("Hazard",     COLS.index("Hz Plan"),    3, "#F55353"),
        ("TBC",        COLS.index("TBC Plan"),   3, "#FF8D29"),
        ("OAK",        COLS.index("OAK Plan"),   3, "#000957"),
        ("Observasi",  COLS.index("Obs Plan"),   3, "#2192FF"),
        ("Coaching",   COLS.index("Coach Plan"), 3, "#F72798"),
        ("Post Event", COLS.index("PE Plan"),    3, "#000000"),
        ("Real Time",  COLS.index("RT Plan"),    3, "#000000"),
    ]

    subheader = []
    for c in COLS:
        if c in LEFT_COLS:
            subheader.append("")
        else:
            if c.endswith("Plan"):
                subheader.append("Plan")
            elif c.endswith("Aktual"):
                subheader.append("Aktual")
            elif c.endswith("Ach"):
                subheader.append("Ach")
            else:
                subheader.append(c)

    fig_w = max(28, ncols * 1.55)
    fig_h = max(6, (nrows + 3) * 0.45)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    plt.subplots_adjust(top=0.86)

    table = ax.table(
        cellText=df.values,
        colLabels=subheader,
        cellLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.35)

    # subheader opacity
    SUB_ALPHA = 0.60

    # default header row
    for j in range(ncols):
        cell = table[(0, j)]
        cell.set_facecolor("#E0E0E0")
        cell.set_text_props(weight="bold")

    # warnai subheader Plan/Aktual/Ach sesuai group (opacity)
    for label, start, span, color in groups:
        rgba = to_rgba(color, alpha=SUB_ALPHA)
        for jj in range(start, start + span):
            cell = table[(0, jj)]
            cell.set_facecolor(rgba)
            cell.set_text_props(weight="bold", color="white")

    # widths
    widths = []
    for col in COLS:
        c = str(col).upper()
        if c == "NAMA":
            widths.append(0.18)
        elif c in ("NIK",):
            widths.append(0.07)
        elif c in ("DEPARTEMEN",):
            widths.append(0.09)
        elif c in ("LAYER", "LAYERING"):
            widths.append(0.08)
        elif c == "ACH TOTAL":
            widths.append(0.08)
        else:
            widths.append(0.04)

    s = sum(widths)
    widths = [w / s for w in widths]
    for j, w in enumerate(widths):
        for i in range(0, nrows + 1):
            table[(i, j)].set_width(w)

    # left align nama
    if "NAMA" in COLS:
        nama_idx = COLS.index("NAMA")
        for i in range(1, nrows + 1):
            cell = table[(i, nama_idx)]
            cell._loc = "left"
            cell.PAD = 0.02
            cell.get_text().set_ha("left")
            cell.get_text().set_va("center")

    # color Ach cells
    ach_cols_idx = [j for j, c in enumerate(COLS) if c.lower().endswith("ach") or "ach total" in c.lower()]
    for i in range(1, nrows + 1):
        for j in ach_cols_idx:
            color = ach_color(df.iloc[i - 1, j])
            if color:
                table[(i, j)].set_facecolor(color)

    fig.canvas.draw()

    header_cell = table[(0, 0)]
    x0, y0 = header_cell.get_xy()
    h = header_cell.get_height()

    def span_rect(col_start, span):
        x_start, _ = table[(0, col_start)].get_xy()
        w_sum = 0.0
        for jj in range(col_start, col_start + span):
            w_sum += table[(0, jj)].get_width()
        return x_start, y0 + h, w_sum, h

    # merge LEFT headers (hitam)
    for col_name in LEFT_COLS:
        j = COLS.index(col_name)
        x, y = table[(0, j)].get_xy()
        w = table[(0, j)].get_width()

        rect = Rectangle((x, y0), w, h * 2, transform=ax.transAxes,
                         facecolor="#000000", edgecolor="black", linewidth=1)
        ax.add_patch(rect)

        ax.text(x + w/2, y0 + h, col_name, transform=ax.transAxes,
                ha="center", va="center", fontsize=12, fontweight="bold", color="white")

        table[(0, j)].get_text().set_text("")

    # top group headers
    for label, start, span, color in groups:
        x, y, w, hh = span_rect(start, span)

        rect = Rectangle((x, y), w, hh, transform=ax.transAxes,
                         facecolor=color, edgecolor="black", linewidth=1)
        ax.add_patch(rect)

        ax.text(x + w/2, y + hh/2, label, transform=ax.transAxes,
                ha="center", va="center", fontsize=11, fontweight="bold", color="white")

    fig.suptitle(title, fontsize=40, fontweight="bold", y=0.85)
    fig.savefig(out_path, dpi=220, bbox_inches="tight")
    plt.close(fig)

def render_all_departemen_png(conn):
    dept_df = pd.read_sql(
        f"""
        SELECT DISTINCT "DEPARTEMEN"
        FROM {FINAL_VIEW}
        WHERE COALESCE("Inspeksi Plan", 0) <> 0
        ORDER BY 1
        """,
        conn
    )

    depts = dept_df["DEPARTEMEN"].dropna().astype(str).tolist()
    if not depts:
        print("Tidak ada departemen yang memenuhi filter.")
        return

    run_dt = datetime.now()
    df = pd.read_sql(SQL_SELECT, conn)

    for dept in depts:
        df_dept = df[df["DEPARTEMEN"].astype(str) == str(dept)].copy()
        if df_dept.empty:
            continue

        title = make_title_png(dept, run_dt)
        out_path = OUT_DIR / f"SAP_{str(dept).replace(' ', '_')}.png"

        render_png(df_dept, title, out_path, max_rows=30)
        print("PNG dibuat:", out_path)


# =========================================================
# FONNTE / FONTE (BLAST WA GROUP)
# =========================================================
FONNTE_TOKEN = os.getenv("FONNTE_TOKEN", "ISI_TOKEN_KAMU_DI_SINI")
FONNTE_SEND_URL = "https://api.fonnte.com/send"

WA_GROUP_TARGETS = [
    "120363407855720163@g.us",
]

def fonnte_send_image(token: str, target: str, message: str, file_path: Path):
    headers = {"Authorization": token}
    data = {"target": target, "message": message}

    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, "image/png")}
        resp = requests.post(
            FONNTE_SEND_URL,
            headers=headers,
            data=data,
            files=files,
            timeout=90
        )

    try:
        payload = resp.json()
    except Exception:
        payload = resp.text

    if resp.status_code >= 400:
        raise RuntimeError(f"FONNTE ERROR {resp.status_code}: {payload}")

    return payload

def blast_png_folder_to_groups(png_dir: Path, title_prefix: str = "Monitoring SAP"):
    png_files = sorted(png_dir.glob("*.png"))
    if not png_files:
        print(f"[FONNTE] Tidak ada PNG di folder: {png_dir}")
        return

    run_dt = datetime.now()

    for target in WA_GROUP_TARGETS:
        print(f"[FONNTE] Blast ke: {target} | total_file={len(png_files)}")

        for fp in png_files:
            stem = fp.stem
            dept_name = stem[4:].replace("_", " ") if stem.upper().startswith("SAP_") else stem.replace("_", " ")

            msg = make_caption_wa_bold(dept_name, run_dt)
            _ = fonnte_send_image(FONNTE_TOKEN, target, msg, fp)
            print(f"[FONNTE] OK target={target} file={fp.name}")

            time.sleep(2)  # delay biar aman (anti spam)


# =========================================================
# MAIN
# =========================================================
def main():
    conn = psycopg2.connect(**PG)
    conn.autocommit = False

    try:
        for zp in RAW_DIR.glob("*.zip"):
            dataset = guess_dataset_from_name(zp.name)
            with zipfile.ZipFile(zp) as z:
                for m in z.namelist():
                    if not m.lower().endswith(".csv"):
                        continue
                    out = EXTRACT_DIR / Path(m).name
                    out.write_bytes(z.read(m))
                    df = normalize_columns(read_csv_robust(out))
                    if dataset == "car":
                        df = normalize_pk_for_car(df)
                    df = filter_rows(dataset, df)
                    upsert_df_to_stg(conn, dataset, df, f"{zp.name}::{m}")

        for ds in FACT_TABLE_MAP:
            upsert_fact(conn, ds)
            refresh_fact_enrichment(conn, ds)

        upsert_absensi_from_gsheets(conn)

        cfg = get_week_config(conn)
        print(f'[WEEK ACTIVE] {cfg["week_key"]} | start_date={cfg["week_start_date"]} | window={cfg["window_start_ts"]}..{cfg["window_end_ts"]}')

        create_final_views(conn)
        conn.commit()

        # render PNG + blast WA grup gaes
        render_all_departemen_png(conn)
        blast_png_folder_to_groups(OUT_DIR, title_prefix="Monitoring SAP")

        print(f"✅ DONE batch_id={BATCH_ID}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
