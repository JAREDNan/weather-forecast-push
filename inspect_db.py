import sys; sys.path.insert(0,'.')
from src.config import load_config
from src.storage.storage import WeatherStorage
import json
cfg=load_config(); st=WeatherStorage(cfg.db_path, cfg.raw_dir)
c=st._conn
def q(sql,*a):
    return [dict(r) for r in c.execute(sql,a).fetchall()]
print("=== 各表数据量 ==="); print(json.dumps(st.daily_stats(),ensure_ascii=False,indent=1))
print("\n=== weather_daily 明细 ===")
for r in q("SELECT forecast_date,temp_min,temp_max,text_day,text_night,is_observation,publish_time,substr(raw_sha256,1,12) sha FROM weather_daily ORDER BY forecast_date"):
    print("  ",r)
print("\n=== 实时观测 ===")
for r in q("SELECT observed_at,temp,humidity,wind_dir,wind_scale,pressure,visibility,aqi,text,raw_file FROM weather_realtime ORDER BY collected_at DESC LIMIT 3"):
    print("  ",r)
print("\n=== 逐小时（前5/共%d）==="%c.execute("SELECT COUNT(*) FROM weather_hourly").fetchone()[0])
for r in q("SELECT hour_ts,temp,humidity,text,wind_dir,wind_deg,pressure FROM weather_hourly ORDER BY hour_ts LIMIT 5"):
    print("  ",r)
print("\n=== 生活指数（按日期统计）===")
for r in q("SELECT index_date,COUNT(*) n FROM weather_index GROUP BY index_date ORDER BY index_date"):
    print("  ",r)
print("\n=== 指数样例 ===")
for r in q("SELECT code,name,level,substr(description,1,40) d FROM weather_index WHERE index_date=(SELECT MIN(index_date) FROM weather_index) LIMIT 6"):
    print("  ",r)
print("\n=== 预警 ==="); print("  条数:",c.execute("SELECT COUNT(*) FROM weather_alarm").fetchone()[0])
print("\n=== 运行日志 ===")
for r in q("SELECT id,started_at,status,sources_ok,raw_files,rows_written FROM collect_run ORDER BY id"):
    print("  ",r)
print("\n=== 缺失率检查 ===")
for col in ("temp_max","temp_min","text_day","text_night","wind_scale_day"):
    tot=c.execute("SELECT COUNT(*) FROM weather_daily WHERE is_observation=0").fetchone()[0]
    nul=c.execute(f"SELECT COUNT(*) FROM weather_daily WHERE is_observation=0 AND {col} IS NULL").fetchone()[0]
    print(f"  {col:<18} 缺失 {nul}/{tot}  ({(nul/tot*100 if tot else 0):.0f}%)")
st.close()
