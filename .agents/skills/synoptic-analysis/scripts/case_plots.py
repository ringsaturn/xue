#!/usr/bin/env python3
"""The standard figure set from a case directory written by case_data.py.

  case_plots.py DIR [--out DIR/figures] [--tz 9] [--lang zh|zh-Hant|en|ja|ko|de|fr|es|pt|tr|ru]
      [--figures tracks,upper,verify,surface,moisture,meteogram,skewt,nowcast,ensemble,fires,section,timeheight,modelskewt]
      [--map-times T1,T2] [--point-name 羽田] [--mark 2026-09-21T08Z] [--window T1,T2]
      [--span T1,T2] [--skewt 47646,47678,47971] [--map-box S N W E] [--borders MNG,CHN]
      [--font "Hiragino Sans GB"] [--section-fill thetae|theta|rh|wind] [--th-hours 120]

Each figure is drawn only when the directory has what it needs (tracks and
ensemble need storm.json / ens.json, meteogram needs point.npz, skewt needs
soundings.json, section / timeheight / modelskewt need columns.npz (a section,
the point's column; `case_data.py fetch --section … --columns`), nowcast needs nowcast.npz, fires needs fires.json, the maps need fields.npz with the
bundles they read: prmsl, hgt500, wind250, wind850, qflux850, thetae850,
prate, wind10m, gust). Needs matplotlib. The coastline is Natural Earth 50 m,
downloaded once into ~/.cache/xue-synoptic/. `--borders MNG,CHN` adds those
countries' outlines from geoBoundaries gbOpen ADM0 at full resolution (cached
the same way). Natural Earth's borders are kilometres off in places (2–4 km on
the China–Mongolia line), too coarse to say which side of a border a fire or a
cell is on; geoBoundaries' accuracy follows its per-country source, which the
run prints, so name the country whose source is OpenStreetMap.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import case_data as C  # noqa: E402
import xue_columns as XC  # noqa: E402

COAST_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_coastline.geojson"
BORDER_API = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/ADM0/"
RAIN_LEVELS = [0.5, 1, 2, 5, 10, 20, 40, 80]
RAIN_COLORS = ["#d9f0ff", "#a6d8ff", "#66b3ff", "#2a80ff", "#ffe066", "#ff9d1a", "#e0261c", "#8e0d8e"]
MODEL_COLORS = {"gfs": "#c0392b", "ecmwf": "#1f5fbf", "ifshres": "#2e8b57", "aifs": "#8e0d8e", "hrrr": "#d35400", "sflux": "#7f8c8d"}
MODEL_NAMES = {"gfs": "GFS", "ecmwf": "ECMWF IFS", "ifshres": "IFS HRES", "aifs": "AIFS", "hrrr": "HRRR", "sflux": "GFS sflux"}
ENS_COLORS = {"gefs": "#f4a6a6", "ecmwfens": "#a6c8f4"}
ENS_NAMES = {"gefs": "GEFS", "ecmwfens": "ECMWF ENS"}

# One label set per UI locale of the xue viewer (zh, zh-Hant, en, ja, ko, de, fr, es, pt, tr, ru);
# station names, model names and units stay as they are.
L = {
    "zh": {"tracks": "{storm} 路径：机构预报、确定性模式与集合成员", "r34": "{ag} 34 kt 大风半径（{t}）", "members": "{m} 成员 ({n})",
           "upper": "500 hPa 位势高度（等值线 gpm，红线 = 5880）与 250 hPa 急流（色阶、风羽）", "jet": "250 hPa 风速 m/s", "valid": "有效 {t}",
           "verify500": "{run} 分析：500 hPa 高度 + 250 hPa 风（数字/风羽 = 探空实测 {t}）", "verify850": "{run} 分析：850 hPa 风 + 500 hPa 高度（数字 = 850 hPa T °C / PW mm）",
           "surface": "海平面气压（等值线 hPa）、10 m 风（风羽 kt）、降水率（色阶）；红虚线 = 10 m 风 ≥ 17.2 m/s", "rain": "降水率 mm/h",
           "moist": "{run} 850 hPa 水汽通量 + θe", "gust": "{run} 10 m 阵风 + 海平面气压", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "阵风 m/s",
           "mslp": "海平面气压 hPa", "wind": "风速 m/s", "rate": "降水率 mm/h", "accum": "累积降水 mm（自各模式起报）", "w10": " 10 m 风", "gustl": " 阵风",
           "gale": "17.2 m/s (烈风)", "strong": "10.8 m/s (强风)", "meteo": "{name}（{lat:.2f}°N {lon:.2f}°E 最近格点）模式气象图，时间 UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "温度 °C（斜线坐标）", "pw": "PW {pw} mm · 冻结高度 {fl} m", "lapse": "850–500 递减率 {lr} K/km · 对流层顶 {tp} m",
           "sat": "{src} {var}  {t}Z（{loc}）", "radar": "{src} {var}  {t}Z（{loc}）", "centre": "{id} 中心 ({ag} {t})",
           "ens_d": "成员路径距 {name} 最近距离 km", "ens_n": "成员数", "ens_t": "最近距离出现时刻（UTC{tz:+g}）", "ens_title": "集合预报：中心最接近 {name} 的距离与时刻（只统计留在区域内的成员）"},
    "zh-Hant": {"tracks": "{storm} 路徑：機構預報、確定性模式與系集成員", "r34": "{ag} 34 kt 暴風半徑（{t}）", "members": "{m} 成員 ({n})",
           "upper": "500 hPa 重力位高度（等值線 gpm，紅線 = 5880）與 250 hPa 噴流（色階、風羽）", "jet": "250 hPa 風速 m/s", "valid": "有效 {t}",
           "verify500": "{run} 分析：500 hPa 高度 + 250 hPa 風（數字/風羽 = 探空實測 {t}）", "verify850": "{run} 分析：850 hPa 風 + 500 hPa 高度（數字 = 850 hPa T °C / PW mm）",
           "surface": "海平面氣壓（等值線 hPa）、10 m 風（風羽 kt）、降水率（色階）；紅虛線 = 10 m 風 ≥ 17.2 m/s", "rain": "降水率 mm/h",
           "moist": "{run} 850 hPa 水汽通量 + θe", "gust": "{run} 10 m 陣風 + 海平面氣壓", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "陣風 m/s",
           "mslp": "海平面氣壓 hPa", "wind": "風速 m/s", "rate": "降水率 mm/h", "accum": "累積降水 mm（自各模式起報）", "w10": " 10 m 風", "gustl": " 陣風",
           "gale": "17.2 m/s (烈風)", "strong": "10.8 m/s (強風)", "meteo": "{name}（{lat:.2f}°N {lon:.2f}°E 最近格點）模式氣象圖，時間 UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "溫度 °C（斜線座標）", "pw": "PW {pw} mm · 凍結高度 {fl} m", "lapse": "850–500 遞減率 {lr} K/km · 對流層頂 {tp} m",
           "sat": "{src} {var}  {t}Z（{loc}）", "radar": "{src} {var}  {t}Z（{loc}）", "centre": "{id} 中心 ({ag} {t})",
           "ens_d": "成員路徑距 {name} 最近距離 km", "ens_n": "成員數", "ens_t": "最近距離出現時刻（UTC{tz:+g}）", "ens_title": "系集預報：中心最接近 {name} 的距離與時刻（只統計留在區域內的成員）"},
    "en": {"tracks": "{storm} track: agency forecast, deterministic models, ensemble members", "r34": "{ag} 34 kt wind radius ({t})", "members": "{m} members ({n})",
           "upper": "500 hPa height (contours gpm, red = 5880) and 250 hPa jet (shading, barbs)", "jet": "250 hPa wind m/s", "valid": "valid {t}",
           "verify500": "{run} analysis: 500 hPa height + 250 hPa wind (numbers/barbs = soundings {t})", "verify850": "{run} analysis: 850 hPa wind + 500 hPa height (numbers = 850 hPa T °C / PW mm)",
           "surface": "MSLP (contours hPa), 10 m wind (barbs kt), rain rate (shading); red dashed = 10 m wind ≥ 17.2 m/s", "rain": "rain rate mm/h",
           "moist": "{run} 850 hPa moisture flux + θe", "gust": "{run} 10 m gust + MSLP", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "gust m/s",
           "mslp": "MSLP hPa", "wind": "wind m/s", "rate": "rain rate mm/h", "accum": "accumulated mm (from each run's start)", "w10": " 10 m wind", "gustl": " gust",
           "gale": "17.2 m/s (gale)", "strong": "10.8 m/s (strong)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E nearest cell) model meteogram, times UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "temperature °C (skewed)", "pw": "PW {pw} mm · freezing level {fl} m", "lapse": "850–500 lapse {lr} K/km · tropopause {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} centre ({ag} {t})",
           "ens_d": "closest approach to {name}, km", "ens_n": "members", "ens_t": "time of closest approach (UTC{tz:+g})", "ens_title": "Ensembles: closest approach to {name} (members that stayed in the box)"},
    "ja": {"tracks": "{storm} 経路：官署予報・決定論モデル・アンサンブルメンバー", "r34": "{ag} 34 kt 暴風域半径（{t}）", "members": "{m} メンバー ({n})",
           "upper": "500 hPa 高度（等値線 gpm、赤 = 5880）と 250 hPa ジェット（陰影・矢羽）", "jet": "250 hPa 風速 m/s", "valid": "対象時刻 {t}",
           "verify500": "{run} 解析：500 hPa 高度 + 250 hPa 風（数値/矢羽 = 高層観測 {t}）", "verify850": "{run} 解析：850 hPa 風 + 500 hPa 高度（数値 = 850 hPa T °C / PW mm）",
           "surface": "海面気圧（等値線 hPa）、10 m 風（矢羽 kt）、降水強度（陰影）；赤破線 = 10 m 風 ≥ 17.2 m/s", "rain": "降水強度 mm/h",
           "moist": "{run} 850 hPa 水蒸気フラックス + θe", "gust": "{run} 10 m 最大瞬間風速 + 海面気圧", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "瞬間風速 m/s",
           "mslp": "海面気圧 hPa", "wind": "風速 m/s", "rate": "降水強度 mm/h", "accum": "積算降水量 mm（各モデルの初期時刻から）", "w10": " 10 m 風", "gustl": " 瞬間風速",
           "gale": "17.2 m/s (暴風)", "strong": "10.8 m/s (強風)", "meteo": "{name}（{lat:.2f}°N {lon:.2f}°E 最近接格子）モデル時系列、時刻 UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "気温 °C（斜交座標）", "pw": "PW {pw} mm · 凍結高度 {fl} m", "lapse": "850–500 気温減率 {lr} K/km · 対流圏界面 {tp} m",
           "sat": "{src} {var}  {t}Z（{loc}）", "radar": "{src} {var}  {t}Z（{loc}）", "centre": "{id} 中心 ({ag} {t})",
           "ens_d": "メンバー経路の {name} への最接近距離 km", "ens_n": "メンバー数", "ens_t": "最接近時刻（UTC{tz:+g}）", "ens_title": "アンサンブル予報：中心が {name} に最接近する距離と時刻（領域内に留まったメンバーのみ）"},
    "ko": {"tracks": "{storm} 경로: 기관 예보, 결정론 모델, 앙상블 멤버", "r34": "{ag} 34 kt 강풍 반경 ({t})", "members": "{m} 멤버 ({n})",
           "upper": "500 hPa 고도(등고선 gpm, 빨간선 = 5880)와 250 hPa 제트(음영, 바람깃)", "jet": "250 hPa 풍속 m/s", "valid": "유효 {t}",
           "verify500": "{run} 분석: 500 hPa 고도 + 250 hPa 바람(숫자/바람깃 = 라디오존데 {t})", "verify850": "{run} 분석: 850 hPa 바람 + 500 hPa 고도(숫자 = 850 hPa T °C / PW mm)",
           "surface": "해면기압(등압선 hPa), 10 m 바람(바람깃 kt), 강수강도(음영); 빨간 점선 = 10 m 바람 ≥ 17.2 m/s", "rain": "강수강도 mm/h",
           "moist": "{run} 850 hPa 수증기 플럭스 + θe", "gust": "{run} 10 m 순간풍속 + 해면기압", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "순간풍속 m/s",
           "mslp": "해면기압 hPa", "wind": "풍속 m/s", "rate": "강수강도 mm/h", "accum": "누적 강수량 mm (각 모델의 초기시각부터)", "w10": " 10 m 바람", "gustl": " 순간풍속",
           "gale": "17.2 m/s (강풍)", "strong": "10.8 m/s (센바람)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E 최근접 격자) 모델 시계열, 시각 UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "기온 °C (경사 좌표)", "pw": "PW {pw} mm · 결빙고도 {fl} m", "lapse": "850–500 기온감률 {lr} K/km · 대류권계면 {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} 중심 ({ag} {t})",
           "ens_d": "멤버 경로의 {name} 최근접 거리 km", "ens_n": "멤버 수", "ens_t": "최근접 시각 (UTC{tz:+g})", "ens_title": "앙상블: 중심이 {name}에 최근접하는 거리와 시각 (영역 안에 남은 멤버만)"},
    "de": {"tracks": "{storm} Zugbahn: Behördenprognose, deterministische Modelle, Ensemblemitglieder", "r34": "{ag} 34-kt-Windradius ({t})", "members": "{m} Mitglieder ({n})",
           "upper": "500 hPa Geopotential (Isohypsen gpm, rot = 5880) und 250 hPa Jet (Schattierung, Windfiedern)", "jet": "250 hPa Wind m/s", "valid": "gültig {t}",
           "verify500": "{run} Analyse: 500 hPa Geopotential + 250 hPa Wind (Zahlen/Fiedern = Radiosonden {t})", "verify850": "{run} Analyse: 850 hPa Wind + 500 hPa Geopotential (Zahlen = 850 hPa T °C / PW mm)",
           "surface": "Bodendruck (Isobaren hPa), 10 m Wind (Fiedern kt), Niederschlagsrate (Schattierung); rot gestrichelt = 10 m Wind ≥ 17,2 m/s", "rain": "Niederschlagsrate mm/h",
           "moist": "{run} 850 hPa Feuchteflussdichte + θe", "gust": "{run} 10 m Böen + Bodendruck", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "Böen m/s",
           "mslp": "Bodendruck hPa", "wind": "Wind m/s", "rate": "Niederschlagsrate mm/h", "accum": "Niederschlagssumme mm (ab Laufbeginn jedes Modells)", "w10": " 10 m Wind", "gustl": " Böen",
           "gale": "17,2 m/s (Sturm)", "strong": "10,8 m/s (starker Wind)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E nächste Gitterzelle) Modellmeteogramm, Zeiten UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "Temperatur °C (schräge Achse)", "pw": "PW {pw} mm · Frostgrenze {fl} m", "lapse": "850–500 Gradient {lr} K/km · Tropopause {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} Zentrum ({ag} {t})",
           "ens_d": "geringste Entfernung zu {name}, km", "ens_n": "Mitglieder", "ens_t": "Zeitpunkt der geringsten Entfernung (UTC{tz:+g})", "ens_title": "Ensembles: geringste Entfernung des Zentrums zu {name} (nur Mitglieder im Gebiet)"},
    "fr": {"tracks": "{storm} trajectoire : prévision officielle, modèles déterministes, membres d'ensemble", "r34": "{ag} rayon des vents de 34 kt ({t})", "members": "{m} membres ({n})",
           "upper": "Géopotentiel 500 hPa (isohypses gpm, rouge = 5880) et jet 250 hPa (plages, barbules)", "jet": "Vent 250 hPa m/s", "valid": "valide {t}",
           "verify500": "{run} analyse : géopotentiel 500 hPa + vent 250 hPa (chiffres/barbules = radiosondages {t})", "verify850": "{run} analyse : vent 850 hPa + géopotentiel 500 hPa (chiffres = T 850 hPa °C / PW mm)",
           "surface": "Pression au niveau de la mer (isobares hPa), vent 10 m (barbules kt), intensité de pluie (plages) ; tirets rouges = vent 10 m ≥ 17,2 m/s", "rain": "intensité de pluie mm/h",
           "moist": "{run} flux d'humidité 850 hPa + θe", "gust": "{run} rafales 10 m + pression mer", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "rafales m/s",
           "mslp": "pression mer hPa", "wind": "vent m/s", "rate": "intensité de pluie mm/h", "accum": "cumul mm (depuis le début de chaque run)", "w10": " vent 10 m", "gustl": " rafales",
           "gale": "17,2 m/s (coup de vent)", "strong": "10,8 m/s (vent fort)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E maille la plus proche) météogramme des modèles, heures UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "température °C (axe oblique)", "pw": "PW {pw} mm · isotherme 0 °C {fl} m", "lapse": "gradient 850–500 {lr} K/km · tropopause {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} centre ({ag} {t})",
           "ens_d": "distance minimale à {name}, km", "ens_n": "membres", "ens_t": "heure de la distance minimale (UTC{tz:+g})", "ens_title": "Ensembles : distance et heure de plus grande approche du centre à {name} (membres restés dans la zone)"},
    "es": {"tracks": "{storm} trayectoria: pronóstico oficial, modelos deterministas, miembros del ensamble", "r34": "{ag} radio de vientos de 34 kt ({t})", "members": "{m} miembros ({n})",
           "upper": "Geopotencial 500 hPa (isohipsas gpm, rojo = 5880) y chorro 250 hPa (sombreado, barbas)", "jet": "Viento 250 hPa m/s", "valid": "válido {t}",
           "verify500": "{run} análisis: geopotencial 500 hPa + viento 250 hPa (cifras/barbas = radiosondeos {t})", "verify850": "{run} análisis: viento 850 hPa + geopotencial 500 hPa (cifras = T 850 hPa °C / PW mm)",
           "surface": "Presión al nivel del mar (isobaras hPa), viento 10 m (barbas kt), intensidad de lluvia (sombreado); rojo discontinuo = viento 10 m ≥ 17,2 m/s", "rain": "intensidad de lluvia mm/h",
           "moist": "{run} flujo de humedad 850 hPa + θe", "gust": "{run} rachas 10 m + presión al nivel del mar", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "rachas m/s",
           "mslp": "presión nivel del mar hPa", "wind": "viento m/s", "rate": "intensidad de lluvia mm/h", "accum": "acumulado mm (desde el inicio de cada corrida)", "w10": " viento 10 m", "gustl": " rachas",
           "gale": "17,2 m/s (temporal)", "strong": "10,8 m/s (viento fuerte)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E celda más cercana) meteograma de modelos, horas UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "temperatura °C (eje oblicuo)", "pw": "PW {pw} mm · nivel de congelación {fl} m", "lapse": "gradiente 850–500 {lr} K/km · tropopausa {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} centro ({ag} {t})",
           "ens_d": "distancia mínima a {name}, km", "ens_n": "miembros", "ens_t": "hora de la distancia mínima (UTC{tz:+g})", "ens_title": "Ensambles: distancia y hora de máxima aproximación del centro a {name} (miembros que permanecieron en el área)"},
    "pt": {"tracks": "{storm} trajetória: previsão oficial, modelos determinísticos, membros do conjunto", "r34": "{ag} raio de ventos de 34 kt ({t})", "members": "{m} membros ({n})",
           "upper": "Geopotencial 500 hPa (isoípsas gpm, vermelho = 5880) e jato 250 hPa (sombreado, barbelas)", "jet": "Vento 250 hPa m/s", "valid": "válido {t}",
           "verify500": "{run} análise: geopotencial 500 hPa + vento 250 hPa (números/barbelas = radiossondagens {t})", "verify850": "{run} análise: vento 850 hPa + geopotencial 500 hPa (números = T 850 hPa °C / PW mm)",
           "surface": "Pressão ao nível do mar (isóbaras hPa), vento 10 m (barbelas kt), taxa de chuva (sombreado); tracejado vermelho = vento 10 m ≥ 17,2 m/s", "rain": "taxa de chuva mm/h",
           "moist": "{run} fluxo de umidade 850 hPa + θe", "gust": "{run} rajadas 10 m + pressão ao nível do mar", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "rajadas m/s",
           "mslp": "pressão nível do mar hPa", "wind": "vento m/s", "rate": "taxa de chuva mm/h", "accum": "acumulado mm (desde o início de cada rodada)", "w10": " vento 10 m", "gustl": " rajadas",
           "gale": "17,2 m/s (vento muito forte)", "strong": "10,8 m/s (vento forte)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E célula mais próxima) meteograma dos modelos, horas UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "temperatura °C (eixo oblíquo)", "pw": "PW {pw} mm · nível de congelamento {fl} m", "lapse": "gradiente 850–500 {lr} K/km · tropopausa {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} centro ({ag} {t})",
           "ens_d": "distância mínima a {name}, km", "ens_n": "membros", "ens_t": "hora da distância mínima (UTC{tz:+g})", "ens_title": "Conjuntos: distância e hora de maior aproximação do centro a {name} (membros que ficaram na área)"},
    "tr": {"tracks": "{storm} izi: resmî tahmin, deterministik modeller, topluluk üyeleri", "r34": "{ag} 34 kt rüzgar yarıçapı ({t})", "members": "{m} üye ({n})",
           "upper": "500 hPa jeopotansiyel (eş yükselti gpm, kırmızı = 5880) ve 250 hPa jet (gölgeleme, rüzgar okları)", "jet": "250 hPa rüzgar m/s", "valid": "geçerli {t}",
           "verify500": "{run} analizi: 500 hPa yükseklik + 250 hPa rüzgar (sayılar/oklar = radyosonda {t})", "verify850": "{run} analizi: 850 hPa rüzgar + 500 hPa yükseklik (sayılar = 850 hPa T °C / PW mm)",
           "surface": "Deniz seviyesi basıncı (izobar hPa), 10 m rüzgar (oklar kt), yağış şiddeti (gölgeleme); kırmızı kesikli = 10 m rüzgar ≥ 17,2 m/s", "rain": "yağış şiddeti mm/sa",
           "moist": "{run} 850 hPa nem akısı + θe", "gust": "{run} 10 m hamle + deniz seviyesi basıncı", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "hamle m/s",
           "mslp": "deniz seviyesi basıncı hPa", "wind": "rüzgar m/s", "rate": "yağış şiddeti mm/sa", "accum": "toplam yağış mm (her modelin başlangıcından)", "w10": " 10 m rüzgar", "gustl": " hamle",
           "gale": "17,2 m/s (fırtına)", "strong": "10,8 m/s (kuvvetli rüzgar)", "meteo": "{name} ({lat:.2f}°K {lon:.2f}°D en yakın hücre) model meteogramı, saatler UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "sıcaklık °C (eğik eksen)", "pw": "PW {pw} mm · donma seviyesi {fl} m", "lapse": "850–500 gradyan {lr} K/km · tropopoz {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} merkez ({ag} {t})",
           "ens_d": "{name} konumuna en yakın geçiş mesafesi, km", "ens_n": "üye sayısı", "ens_t": "en yakın geçiş zamanı (UTC{tz:+g})", "ens_title": "Topluluklar: merkezin {name} konumuna en yakın geçiş mesafesi ve zamanı (alanda kalan üyeler)"},
    "ru": {"tracks": "{storm} траектория: официальный прогноз, детерминированные модели, члены ансамбля", "r34": "{ag} радиус ветра 34 kt ({t})", "members": "{m} членов ({n})",
           "upper": "Геопотенциал 500 гПа (изогипсы гп. м, красная = 5880) и струйное течение 250 гПа (заливка, флажки)", "jet": "Ветер 250 гПа м/с", "valid": "срок {t}",
           "verify500": "{run} анализ: высота 500 гПа + ветер 250 гПа (цифры/флажки = радиозонды {t})", "verify850": "{run} анализ: ветер 850 гПа + высота 500 гПа (цифры = T 850 гПа °C / PW мм)",
           "surface": "Давление на уровне моря (изобары гПа), ветер 10 м (флажки kt), интенсивность осадков (заливка); красный пунктир = ветер 10 м ≥ 17,2 м/с", "rain": "интенсивность осадков мм/ч",
           "moist": "{run} поток влаги 850 гПа + θe", "gust": "{run} порывы 10 м + давление на уровне моря", "qflux": "|q·V/g| г/(см·гПа·с)", "gustcb": "порывы м/с",
           "mslp": "давление на уровне моря гПа", "wind": "ветер м/с", "rate": "интенсивность осадков мм/ч", "accum": "сумма осадков мм (от начала каждого прогона)", "w10": " ветер 10 м", "gustl": " порывы",
           "gale": "17,2 м/с (штормовой)", "strong": "10,8 м/с (сильный)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E ближайшая ячейка) метеограмма моделей, время UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "температура °C (косая ось)", "pw": "PW {pw} мм · уровень замерзания {fl} м", "lapse": "градиент 850–500 {lr} K/км · тропопауза {tp} м",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} центр ({ag} {t})",
           "ens_d": "минимальное расстояние до {name}, км", "ens_n": "членов", "ens_t": "время минимального расстояния (UTC{tz:+g})", "ens_title": "Ансамбли: расстояние и время наибольшего сближения центра с {name} (члены, оставшиеся в области)"},
}


# Column-store figures (section, time-height, model sounding), one set per UI locale.
COLUMN_LABELS = {
    "zh": {"sec": "{run} 剖面 {name}，有效 {t}", "sec_note": "等值线：θ（灰，4 K）· 垂直剖面的风 ≥ 20（m/s，实线 = 出纸面，虚线 = 入纸面）· 0 °C（蓝）",
           "dist": "距 A 点 km", "th": "{run} {name}（{lat:.2f}°N {lon:.2f}°E）时间–高度剖面：RH（色阶）、T（等值线 °C，蓝 = 0 °C）、风（风羽 kt）",
           "time": "时间 UTC{tz:+g}", "mskewt": "{run} 模式探空 {name}（{lat:.2f}, {lon:.2f}）{t}", "ground": "地形",
           "idx": "PW {pw} mm · 0 °C 高度 {fl} m · LCL {lcl} m · K {k} · 0–6 km 切变 {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "风速 m/s"}},
    "zh-Hant": {"sec": "{run} 剖面 {name}，有效 {t}", "sec_note": "等值線：θ（灰，4 K）· 垂直剖面的風 ≥ 20（m/s，實線 = 出紙面，虛線 = 入紙面）· 0 °C（藍）",
           "dist": "距 A 點 km", "th": "{run} {name}（{lat:.2f}°N {lon:.2f}°E）時間–高度剖面：RH（色階）、T（等值線 °C，藍 = 0 °C）、風（風羽 kt）",
           "time": "時間 UTC{tz:+g}", "mskewt": "{run} 模式探空 {name}（{lat:.2f}, {lon:.2f}）{t}", "ground": "地形",
           "idx": "PW {pw} mm · 0 °C 高度 {fl} m · LCL {lcl} m · K {k} · 0–6 km 風切 {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "風速 m/s"}},
    "en": {"sec": "{run} cross-section {name}, valid {t}", "sec_note": "contours: θ (grey, 4 K) · wind across the section ≥ 20 (m/s, solid = out of page, dashed = into page) · 0 °C (blue)",
           "dist": "distance from A, km", "th": "{run} time-height at {name} ({lat:.2f}°N {lon:.2f}°E): RH (shading), T (contours °C, blue = 0 °C), wind (barbs kt)",
           "time": "time UTC{tz:+g}", "mskewt": "{run} model sounding {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "terrain",
           "idx": "PW {pw} mm · 0 °C level {fl} m · LCL {lcl} m · K {k} · 0–6 km shear {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "wind speed m/s"}},
    "ja": {"sec": "{run} 鉛直断面 {name}、対象時刻 {t}", "sec_note": "等値線：θ（灰、4 K）・断面に直交する風 ≥ 20（m/s、実線 = 紙面手前向き、破線 = 奥向き）・0 °C（青）",
           "dist": "A 点からの距離 km", "th": "{run} {name}（{lat:.2f}°N {lon:.2f}°E）時間高度断面：RH（陰影）、T（等値線 °C、青 = 0 °C）、風（矢羽 kt）",
           "time": "時刻 UTC{tz:+g}", "mskewt": "{run} モデル予想エマグラム {name}（{lat:.2f}, {lon:.2f}）{t}", "ground": "地形",
           "idx": "PW {pw} mm · 0 °C 高度 {fl} m · LCL {lcl} m · K {k} · 0–6 km シア {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "風速 m/s"}},
    "ko": {"sec": "{run} 연직단면 {name}, 유효 {t}", "sec_note": "등치선: θ(회색, 4 K) · 단면에 수직인 바람 ≥ 20(m/s, 실선 = 지면 앞쪽, 점선 = 뒤쪽) · 0 °C(파랑)",
           "dist": "A 지점으로부터 거리 km", "th": "{run} {name} ({lat:.2f}°N {lon:.2f}°E) 시간-고도 단면: RH(음영), T(등치선 °C, 파랑 = 0 °C), 바람(바람깃 kt)",
           "time": "시각 UTC{tz:+g}", "mskewt": "{run} 모델 연직 프로파일 {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "지형",
           "idx": "PW {pw} mm · 0 °C 고도 {fl} m · LCL {lcl} m · K {k} · 0–6 km 시어 {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "풍속 m/s"}},
    "de": {"sec": "{run} Vertikalschnitt {name}, gültig {t}", "sec_note": "Isolinien: θ (grau, 4 K) · Wind quer zum Schnitt ≥ 20 (m/s, durchgezogen = aus der Ebene, gestrichelt = in die Ebene) · 0 °C (blau)",
           "dist": "Entfernung von A, km", "th": "{run} Zeit-Höhen-Schnitt {name} ({lat:.2f}°N {lon:.2f}°E): RH (Schattierung), T (Isolinien °C, blau = 0 °C), Wind (Fiedern kt)",
           "time": "Zeit UTC{tz:+g}", "mskewt": "{run} Modellsondierung {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "Gelände",
           "idx": "PW {pw} mm · Nullgradgrenze {fl} m · LCL {lcl} m · K {k} · 0–6 km Scherung {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "Windgeschwindigkeit m/s"}},
    "fr": {"sec": "{run} coupe verticale {name}, valide {t}", "sec_note": "isolignes : θ (gris, 4 K) · vent normal à la coupe ≥ 20 (m/s, plein = sortant, tirets = entrant) · 0 °C (bleu)",
           "dist": "distance depuis A, km", "th": "{run} coupe temps-altitude {name} ({lat:.2f}°N {lon:.2f}°E) : HR (plages), T (isolignes °C, bleu = 0 °C), vent (barbules kt)",
           "time": "heure UTC{tz:+g}", "mskewt": "{run} sondage modèle {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "relief",
           "idx": "PW {pw} mm · isotherme 0 °C {fl} m · LCL {lcl} m · K {k} · cisaillement 0–6 km {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "HR %", "wind": "vitesse du vent m/s"}},
    "es": {"sec": "{run} corte vertical {name}, válido {t}", "sec_note": "isolíneas: θ (gris, 4 K) · viento normal al corte ≥ 20 (m/s, continuo = saliente, discontinuo = entrante) · 0 °C (azul)",
           "dist": "distancia desde A, km", "th": "{run} corte tiempo-altura {name} ({lat:.2f}°N {lon:.2f}°E): HR (sombreado), T (isolíneas °C, azul = 0 °C), viento (barbas kt)",
           "time": "hora UTC{tz:+g}", "mskewt": "{run} sondeo del modelo {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "relieve",
           "idx": "PW {pw} mm · nivel de 0 °C {fl} m · LCL {lcl} m · K {k} · cizalladura 0–6 km {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "HR %", "wind": "velocidad del viento m/s"}},
    "pt": {"sec": "{run} corte vertical {name}, válido {t}", "sec_note": "isolinhas: θ (cinza, 4 K) · vento normal ao corte ≥ 20 (m/s, contínua = saindo, tracejada = entrando) · 0 °C (azul)",
           "dist": "distância de A, km", "th": "{run} corte tempo-altura {name} ({lat:.2f}°N {lon:.2f}°E): UR (sombreado), T (isolinhas °C, azul = 0 °C), vento (barbelas kt)",
           "time": "hora UTC{tz:+g}", "mskewt": "{run} sondagem do modelo {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "relevo",
           "idx": "PW {pw} mm · nível de 0 °C {fl} m · LCL {lcl} m · K {k} · cisalhamento 0–6 km {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "UR %", "wind": "velocidade do vento m/s"}},
    "tr": {"sec": "{run} düşey kesit {name}, geçerli {t}", "sec_note": "eş değer eğrileri: θ (gri, 4 K) · kesite dik rüzgar ≥ 20 (m/s, düz = sayfadan dışarı, kesikli = içeri) · 0 °C (mavi)",
           "dist": "A noktasından uzaklık, km", "th": "{run} zaman-yükseklik kesiti {name} ({lat:.2f}°K {lon:.2f}°D): BN (gölgeleme), T (eğriler °C, mavi = 0 °C), rüzgar (oklar kt)",
           "time": "saat UTC{tz:+g}", "mskewt": "{run} model sondajı {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "arazi",
           "idx": "PW {pw} mm · 0 °C seviyesi {fl} m · LCL {lcl} m · K {k} · 0–6 km kesme {sh} m/s · CAPE {cape} J/kg",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "BN %", "wind": "rüzgar hızı m/s"}},
    "ru": {"sec": "{run} вертикальный разрез {name}, срок {t}", "sec_note": "изолинии: θ (серые, 4 K) · ветер поперёк разреза ≥ 20 (м/с, сплошные = от листа, пунктир = в лист) · 0 °C (синяя)",
           "dist": "расстояние от A, км", "th": "{run} разрез время–высота {name} ({lat:.2f}°N {lon:.2f}°E): RH (заливка), T (изолинии °C, синяя = 0 °C), ветер (флажки kt)",
           "time": "время UTC{tz:+g}", "mskewt": "{run} модельное зондирование {name} ({lat:.2f}, {lon:.2f}) {t}", "ground": "рельеф",
           "idx": "PW {pw} мм · уровень 0 °C {fl} м · LCL {lcl} м · K {k} · сдвиг 0–6 км {sh} м/с · CAPE {cape} Дж/кг",
           "fill": {"thetae": "θe K", "theta": "θ K", "rh": "RH %", "wind": "скорость ветра м/с"}},
}
SECTION_FILLS = {"thetae": ("Spectral_r", 4), "theta": ("Spectral_r", 4), "rh": ("BrBG", 10), "wind": ("YlOrRd", 5)}
PRESSURE_TICKS = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100]


class Case:
    def __init__(self, d: Path, tz: float, lang: str, borders: tuple[str, ...] = ()):
        self.d = d; self.tz = timezone(timedelta(hours=tz)); self.tzh = tz; self.T = L[lang]; self.lang = lang
        self.meta = json.loads((d / "meta.json").read_text())
        self.fields = np.load(d / "fields.npz") if (d / "fields.npz").exists() else None
        self.point = np.load(d / "point.npz") if (d / "point.npz").exists() else None
        self.now = np.load(d / "nowcast.npz") if (d / "nowcast.npz").exists() else None
        self.fires = C._load_json(d, "fires.json")
        self.column, self.sections = XC.load_case(d / "columns.npz") if (d / "columns.npz").exists() else (None, [])
        self.CT = COLUMN_LABELS[lang]
        self.storm = C._load_json(d, "storm.json"); self.ens = C._load_json(d, "ens.json"); self.snd = C._load_json(d, "soundings.json")
        self.coast = load_coast()
        self.borders = [ln for iso in borders for ln in load_border(iso)]

    def local(self, T: str, fmt="%m/%d %H") -> str:
        return C.parse_time(T).astimezone(self.tz).strftime(fmt) + (f" UTC{self.tzh:+g}" if self.tzh else "Z")

    def run_label(self, src: str) -> str:
        run = self.meta["runs"].get(src, src)
        return f"{MODEL_NAMES.get(src, src)} {run.split('.')[1][:8]}/{run.split('.')[1][8:10]}Z" if "." in run else run

    def fld(self, src, b, v, T):
        f = self.fields
        return f[f"{src}:{b}:{v}:{T}"], f[f"{src}:{b}:lat"], f[f"{src}:{b}:lon"]

    def has(self, src, b, T):
        return self.fields is not None and any(k.startswith(f"{src}:{b}:") and k.endswith(T) for k in self.fields.files)


def load_coast():
    cache = Path.home() / ".cache" / "xue-synoptic" / "ne_50m_coastline.geojson"
    if not cache.exists():
        import requests
        cache.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(COAST_URL, timeout=120); r.raise_for_status(); cache.write_bytes(r.content)
    g = json.loads(cache.read_text())
    lines = []
    for feat in g["features"]:
        geom = feat["geometry"]
        for ln in (geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]):
            lines.append(np.asarray(ln))
    return lines


def load_border(iso: str):
    """One country's outline as polygon rings, from its geoBoundaries ADM0."""
    iso = iso.upper()
    cache = Path.home() / ".cache" / "xue-synoptic" / f"geoBoundaries-{iso}-ADM0.geojson"
    import requests
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        meta = requests.get(BORDER_API.format(iso=iso), timeout=60); meta.raise_for_status(); meta = meta.json()
        print(f"borders {iso}: {meta.get('boundarySource')} ({meta.get('boundaryLicense')})", file=sys.stderr)
        r = requests.get(meta["gjDownloadURL"], timeout=300); r.raise_for_status(); cache.write_bytes(r.content)
    rings = []
    for feat in json.loads(cache.read_text())["features"]:
        geom = feat["geometry"]
        for poly in (geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]):
            rings.extend(np.asarray(ring) for ring in poly)
    return rings


# --- map scaffolding ----------------------------------------------------------

def setmap(ax, box, case: Case, title=None, marks=()):
    s, n, w, e = box
    for a in case.coast:
        if a[:, 0].max() < w - 5 or a[:, 0].min() > e + 5 or a[:, 1].max() < s - 5 or a[:, 1].min() > n + 5:
            continue
        ax.plot(a[:, 0], a[:, 1], color="0.25", lw=0.6, zorder=5)
    for a in case.borders:
        if a[:, 0].max() < w - 5 or a[:, 0].min() > e + 5 or a[:, 1].max() < s - 5 or a[:, 1].min() > n + 5:
            continue
        ax.plot(a[:, 0], a[:, 1], color="0.25", lw=0.6, ls=(0, (4, 2)), zorder=5)
    ax.set_xlim(w, e); ax.set_ylim(s, n)
    ax.set_aspect(1 / math.cos(math.radians((s + n) / 2)))
    step = 5 if e - w <= 40 else 10
    ax.set_xticks(np.arange(math.ceil(w / step) * step, e + 0.1, step)); ax.set_yticks(np.arange(math.ceil(s / step) * step, n + 0.1, step))
    ax.set_xticklabels([f"{int(x)}°E" if x <= 180 else f"{int(360 - x)}°W" for x in ax.get_xticks()], fontsize=7)
    ax.set_yticklabels([f"{int(y)}°N" if y >= 0 else f"{int(-y)}°S" for y in ax.get_yticks()], fontsize=7)
    ax.grid(True, lw=0.3, color="0.7", alpha=0.6)
    for lat, lon in marks:
        ax.plot(lon, lat, marker="*", ms=11, color="gold", mec="k", mew=0.8, zorder=12)
    if title:
        ax.set_title(title, fontsize=9)


def barbs(ax, lon, lat, u, v, st, **kw):
    ax.barbs(lon[::st], lat[::st], u[::st, ::st] * 1.944, v[::st, ::st] * 1.944, length=kw.pop("length", 4.2), linewidth=kw.pop("linewidth", 0.4), color=kw.pop("color", "0.3"), zorder=6, **kw)


# --- figures ---------------------------------------------------------------------

def fig_tracks(case: Case, out: Path, box, marks, plt):
    st = case.storm; T = case.T
    fig, ax = plt.subplots(figsize=(8.2, 6.6))
    setmap(ax, box, case, T["tracks"].format(storm=f"{st['id']} ({st.get('name') or ''})"), marks)
    for k, v in (case.ens or {}).items():
        for t in v["tracks"]:
            pts = [p for p in t["points"] if box[0] - 5 <= p["lat"] <= box[1] + 5 and box[2] - 5 <= p["lon"] <= box[3] + 5]
            if len(pts) >= 3:
                ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts], color=ENS_COLORS.get(k, "0.8"), lw=0.6, alpha=0.8, zorder=3)
        ax.plot([], [], color=ENS_COLORS.get(k, "0.8"), lw=1, label=T["members"].format(m=ENS_NAMES.get(k, k), n=len(v["tracks"])))
    for k, v in st.get("models", {}).items():
        if "members" in v:
            continue
        pts = [p for p in v["points"] if p["lead"] <= 120 * 3600]
        col = MODEL_COLORS.get(k, "0.4")
        ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts], color=col, lw=1.6, zorder=8, label=f"{MODEL_NAMES.get(k, k)} {v.get('run', '')}")
        for p in pts:
            if p["lead"] % 43200 == 0:
                ax.plot(p["lon"], p["lat"], "o", ms=3.5, color=col, zorder=9)
    for ag, v in st.get("agencies", {}).items():
        pts = v["points"]
        ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts], color="k", lw=2.0, zorder=10, label=f"{ag.upper()} #{v.get('number', '')} ({v['issued'][5:16]}Z)")
        for p in pts:
            ax.plot(p["lon"], p["lat"], "s", ms=5, color="k", zorder=11)
            t = datetime.strptime(p["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(case.tz)
            vm = f"\n{p['vmax'] * 1.944:.0f} kt" if p.get("vmax") else ""
            ax.annotate(f"{t.strftime('%d/%H')}{vm}", (p["lon"], p["lat"]), xytext=(6, -4), textcoords="offset points", fontsize=6.5, zorder=12,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))
        # 34 kt radii at the point closest to the map's first mark
        if marks:
            near = min((p for p in pts if p.get("radii", {}).get("34")), key=lambda p: C.km(marks[0][0], marks[0][1], p["lat"], p["lon"]), default=None)
            if near:
                r = near["radii"]["34"]; th = np.linspace(0, 2 * np.pi, 181)
                quad = [r[int(a // (np.pi / 2)) % 4] for a in th]
                ys = [near["lat"] + q / 111 * np.cos(a) for q, a in zip(quad, th)]
                xs = [near["lon"] + q / (111 * math.cos(math.radians(near["lat"]))) * np.sin(a) for q, a in zip(quad, th)]
                ax.plot(xs, ys, color="k", lw=0.9, ls="--", zorder=9, label=T["r34"].format(ag=ag.upper(), t=case.local(near["time"][:16] + "Z")))
        break
    ax.legend(loc="lower right", fontsize=7, framealpha=0.9)
    fig.tight_layout(); fig.savefig(out / "fig_tracks.png"); plt.close(fig)


def fig_upper(case: Case, out: Path, box, marks, times, plt):
    T = case.T; srcs = [s for s in case.meta["sources"] if all(case.has(s, "hgt500", t) and case.has(s, "wind250", t) for t in times)]
    if not srcs:
        return
    fig, axs = plt.subplots(len(srcs), len(times), figsize=(5.5 * len(times), 4.1 * len(srcs)), squeeze=False)
    for i, src in enumerate(srcs):
        for j, t in enumerate(times):
            ax = axs[i, j]; z, lat, lon = case.fld(src, "hgt500", "hgt500", t); u, _, _ = case.fld(src, "wind250", "ugrd250", t); v, _, _ = case.fld(src, "wind250", "vgrd250", t)
            setmap(ax, box, case, f"{case.run_label(src)}\n{T['valid'].format(t=case.local(t))}", marks)
            cf = ax.contourf(lon, lat, np.hypot(u, v), levels=[30, 40, 50, 60, 70, 80], cmap="Purples", alpha=0.75, extend="max", zorder=1)
            cs = ax.contour(lon, lat, z, levels=np.arange(4800, 6040, 40), colors="k", linewidths=0.6, zorder=6); ax.clabel(cs, fmt="%d", fontsize=6)
            ax.contour(lon, lat, z, levels=[5880], colors="#c0392b", linewidths=1.6, zorder=7)
            barbs(ax, lon, lat, u, v, max(1, len(lon) // 20))
            section_lines(ax, case)
    fig.colorbar(cf, ax=axs, shrink=0.6, label=T["jet"], pad=0.02); fig.suptitle(T["upper"], fontsize=10)
    fig.savefig(out / "fig_upper.png", bbox_inches="tight"); plt.close(fig)


def fig_verify(case: Case, out: Path, box, marks, plt):
    T = case.T; src = case.meta["sources"][0]; t = case.meta["times"][0]
    if not (case.snd and case.has(src, "hgt500", t)):
        return
    fig, axs = plt.subplots(1, 2, figsize=(12, 5.2))
    for ax, lev in zip(axs, ("500", "850")):
        z, lat, lon = case.fld(src, "hgt500", "hgt500", t)
        bw = "wind250" if lev == "500" else "wind850"
        if not case.has(src, bw, t):
            continue
        u, _, _ = case.fld(src, bw, "ugrd" + bw[4:], t); v, _, _ = case.fld(src, bw, "vgrd" + bw[4:], t)
        setmap(ax, box, case, T["verify" + lev].format(run=case.run_label(src), t=case.local(t)), marks)
        cs = ax.contour(lon, lat, z, levels=np.arange(4800, 6040, 20), colors="0.5", linewidths=0.5, zorder=4); ax.clabel(cs, levels=np.arange(4800, 6040, 40), fmt="%d", fontsize=6)
        ax.contour(lon, lat, z, levels=[5880], colors="#c0392b", linewidths=1.4, zorder=5)
        barbs(ax, lon, lat, u, v, max(1, len(lon) // 24), color="0.55", linewidth=0.35, length=4)
        for w, s in case.snd.items():
            asc = C.ascent_at(s, t)
            if not asc:
                continue
            r = C.level(asc, 50000 if lev == "500" else 85000); rw = C.level(asc, 25000 if lev == "500" else 85000)
            if r is None:
                continue
            ax.plot(s["lon"], s["lat"], "o", ms=3, color="#1f5fbf", zorder=10)
            txt = f"{r['z']}" if lev == "500" else f"{r['t']:.0f}/{asc.get('derived', {}).get('pw', '')}"
            ax.annotate(txt, (s["lon"], s["lat"]), xytext=(4, 3), textcoords="offset points", fontsize=7, color="#1f5fbf", zorder=11, fontweight="bold")
            if rw and rw["wd"] is not None and rw["ws"] is not None:
                ang = math.radians(rw["wd"]); sp = rw["ws"] * 1.944
                ax.barbs([s["lon"]], [s["lat"]], [-sp * math.sin(ang)], [-sp * math.cos(ang)], length=5.5, linewidth=0.9, color="#1f5fbf", zorder=11)
    fig.tight_layout(); fig.savefig(out / "fig_verify.png"); plt.close(fig)


def fig_surface(case: Case, out: Path, box, marks, times, plt):
    from matplotlib.colors import BoundaryNorm, ListedColormap
    T = case.T; cm = ListedColormap(RAIN_COLORS); norm = BoundaryNorm(RAIN_LEVELS, cm.N)
    srcs = [s for s in case.meta["sources"] if all(case.has(s, "prmsl", t) and case.has(s, "wind10m", t) for t in times)]
    if not srcs:
        return
    fig, axs = plt.subplots(len(srcs), len(times), figsize=(5.5 * len(times), 4.5 * len(srcs)), squeeze=False)
    for i, src in enumerate(srcs):
        for j, t in enumerate(times):
            ax = axs[i, j]; p, lat, lon = case.fld(src, "prmsl", "prmsl", t); u, _, _ = case.fld(src, "wind10m", "ugrd10m", t); v, _, _ = case.fld(src, "wind10m", "vgrd10m", t)
            setmap(ax, box, case, f"{case.run_label(src)}\n{T['valid'].format(t=case.local(t))}", marks)
            if case.has(src, "prate", t):
                r, _, _ = case.fld(src, "prate", "prate", t)
                cf = ax.contourf(lon, lat, r, levels=RAIN_LEVELS, cmap=cm, norm=norm, extend="max", zorder=1)
            cs = ax.contour(lon, lat, p, levels=np.arange(900, 1060, 4), colors="k", linewidths=0.6, zorder=6); ax.clabel(cs, fmt="%d", fontsize=6)
            barbs(ax, lon, lat, u, v, max(1, len(lon) // 22), color="0.2")
            ii, kk = np.unravel_index(np.nanargmin(p), p.shape)
            ax.text(lon[kk], lat[ii], "L", fontsize=13, fontweight="bold", color="#c0392b", ha="center", va="center", zorder=12)
            ax.text(lon[kk], lat[ii] - (box[1] - box[0]) / 20, f"{p[ii, kk]:.0f}", fontsize=7, color="#c0392b", ha="center", zorder=12)
            ax.contour(lon, lat, np.hypot(u, v), levels=[17.2], colors="#c0392b", linewidths=1.0, linestyles="--", zorder=7)
    fig.colorbar(cf, ax=axs, shrink=0.6, label=T["rain"], pad=0.02); fig.suptitle(T["surface"], fontsize=10)
    fig.savefig(out / "fig_surface.png", bbox_inches="tight"); plt.close(fig)


def fig_moisture(case: Case, out: Path, box, marks, times, plt):
    T = case.T; t1 = times[0]; t2 = times[-1]
    srcs = [s for s in case.meta["sources"] if case.has(s, "qflux850", t1) and case.has(s, "gust", t2) and case.has(s, "prmsl", t2)]
    if not srcs:
        return
    fig, axs = plt.subplots(len(srcs), 2, figsize=(11, 4.5 * len(srcs)), squeeze=False)
    for i, src in enumerate(srcs):
        ax = axs[i, 0]; qu, lat, lon = case.fld(src, "qflux850", "uqflx850", t1); qv, _, _ = case.fld(src, "qflux850", "vqflx850", t1)
        setmap(ax, box, case, f"{T['moist'].format(run=case.run_label(src))}\n{T['valid'].format(t=case.local(t1))}", marks)
        cf = ax.contourf(lon, lat, np.hypot(qu, qv), levels=[10, 15, 20, 30, 40, 60, 80], cmap="YlGnBu", extend="max", zorder=1)
        st = max(1, len(lon) // 22)
        ax.quiver(lon[::st], lat[::st], qu[::st, ::st], qv[::st, ::st], scale=900, width=0.0025, color="0.25", zorder=6)
        if case.has(src, "thetae850", t1):
            te, _, _ = case.fld(src, "thetae850", "thetae850", t1)
            cs = ax.contour(lon, lat, te, levels=np.arange(280, 370, 4), colors="#8e0d8e", linewidths=0.6, zorder=5); ax.clabel(cs, fmt="%d", fontsize=6)
        ax = axs[i, 1]; g, lat, lon = case.fld(src, "gust", "gust", t2); p, _, _ = case.fld(src, "prmsl", "prmsl", t2)
        setmap(ax, box, case, f"{T['gust'].format(run=case.run_label(src))}\n{T['valid'].format(t=case.local(t2))}", marks)
        cg = ax.contourf(lon, lat, g, levels=[10, 15, 20, 25, 30, 35, 40, 50], cmap="OrRd", extend="max", zorder=1)
        cs = ax.contour(lon, lat, p, levels=np.arange(900, 1060, 4), colors="k", linewidths=0.5, zorder=6); ax.clabel(cs, fmt="%d", fontsize=6)
    fig.colorbar(cf, ax=axs[:, 0], shrink=0.6, label=T["qflux"], pad=0.02); fig.colorbar(cg, ax=axs[:, 1], shrink=0.6, label=T["gustcb"], pad=0.02)
    fig.savefig(out / "fig_moisture_gust.png", bbox_inches="tight"); plt.close(fig)


def fig_meteogram(case: Case, out: Path, name, mark, window, span, plt):
    import matplotlib.dates as mdates
    T = case.T; pt = case.point; lat, lon = case.meta["point"]
    fig, axs = plt.subplots(4, 1, figsize=(10, 10.5), sharex=True)
    t0 = t1 = None
    for src in case.meta["point_sources"]:
        fr = C.point_frame(pt, src, case.tzh)
        if not fr:
            continue
        col = MODEL_COLORS.get(src, "0.4"); lab = case.run_label(src); t = fr["t"]
        t0 = min(t0, t[0]) if t0 else t[0]; t1 = max(t1, t[-1]) if t1 else t[-1]
        axs[0].plot(t, fr["p"], color=col, lw=1.4, label=lab)
        axs[1].plot(t, fr["ws"], color=col, lw=1.4, label=lab + T["w10"]); axs[1].plot(t, fr["gust"], color=col, lw=1.0, ls="--", label=lab + T["gustl"])
        axs[2].plot(t, fr["rate"], color=col, lw=1.4, label=lab, drawstyle="steps-post")
        axs[3].plot(t, C.accumulate(t, fr["rate"]), color=col, lw=1.4, label=lab)
    axs[0].set_ylabel(T["mslp"]); axs[1].set_ylabel(T["wind"]); axs[2].set_ylabel(T["rate"]); axs[3].set_ylabel(T["accum"])
    axs[1].axhline(17.2, color="0.4", lw=0.6, ls=":"); axs[1].text(0.01, 17.6, T["gale"], fontsize=7, color="0.3", transform=axs[1].get_yaxis_transform())
    axs[1].axhline(10.8, color="0.6", lw=0.6, ls=":"); axs[1].text(0.01, 11.2, T["strong"], fontsize=7, color="0.4", transform=axs[1].get_yaxis_transform())
    for ax in axs:
        if window:
            ax.axvspan(C.parse_time(window[0]).astimezone(case.tz), C.parse_time(window[1]).astimezone(case.tz), color="gold", alpha=0.25, lw=0)
        if mark:
            ax.axvline(C.parse_time(mark).astimezone(case.tz), color="k", lw=1.0, ls="-.")
        ax.grid(True, lw=0.3, alpha=0.6); ax.legend(fontsize=7, loc="upper left", ncol=2 if ax is axs[1] else 1)
    if mark:
        m = C.parse_time(mark).astimezone(case.tz); axs[0].text(m, axs[0].get_ylim()[0] + 1, T["mark"].format(t=m.strftime("%d %H:%M")), fontsize=8, va="bottom")
    axs[0].set_title(T["meteo"].format(name=name, lat=lat, lon=lon, tz=case.tzh), fontsize=9)
    axs[3].xaxis.set_major_formatter(mdates.DateFormatter("%d/%H", tz=case.tz)); axs[3].xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=case.tz) if ((C.parse_time(span[1]) - C.parse_time(span[0])).days if span else (t1 - t0).days) <= 5 else mdates.DayLocator(tz=case.tz))
    if span:
        t0, t1 = (C.parse_time(x).astimezone(case.tz) for x in span)
    plt.setp(axs[3].get_xticklabels(), rotation=45, ha="right", fontsize=7); axs[3].set_xlim(t0, t1)
    fig.tight_layout(); fig.savefig(out / "fig_meteogram.png"); plt.close(fig)


def skewt(ax, asc: dict, title: str, T: dict, skew=35.0):
    p, z, tt, td, wd, ws = C.profile(asc)
    d = asc.get("derived", {})
    note = T["pw"].format(pw=d.get("pw"), fl=d.get("freezingLevel")) + "\n" + T["lapse"].format(lr=d.get("lapse850_500"), tp=d.get("tropopause"))
    skewt_arrays(ax, p, tt, td, wd, ws, title, T, note, skew)


def skewt_arrays(ax, p, tt, td, wd, ws, title: str, T: dict, note: str, skew=35.0, mandatory_barbs=False):
    X = lambda temp, pp: temp + skew * (np.log(1000) - np.log(pp))  # noqa: E731
    for T0 in range(-100, 60, 10):
        ax.plot([X(T0, 1000), X(T0, 100)], [np.log(1000), np.log(100)], color="0.85", lw=0.5, zorder=1)
    for th in range(250, 460, 20):
        pp = np.linspace(1000, 100, 50); ax.plot(X(th * (pp / 1000) ** 0.2857 - 273.15, pp), np.log(pp), color="#e8c9a0", lw=0.5, zorder=1)
    y = np.log(p)
    ax.plot(X(tt, p), y, color="#c0392b", lw=1.6, zorder=5, label="T"); ax.plot(X(td, p), y, color="#2e8b57", lw=1.6, zorder=5, label="Td")
    ticks = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100]
    ax.set_yticks(np.log(ticks)); ax.set_yticklabels(ticks, fontsize=7); ax.set_ylim(np.log(1050), np.log(100)); ax.set_xlim(-40, 45)
    ax.set_xticks(range(-40, 50, 10)); ax.set_xticklabels(range(-40, 50, 10), fontsize=7); ax.set_xlabel(T["skewt_x"], fontsize=8); ax.set_ylabel("hPa", fontsize=8)
    sel = [i for i in range(len(p)) if not np.isnan(wd[i]) and not np.isnan(ws[i]) and (p[i] in ticks or (i % 6 == 0 and not mandatory_barbs))]
    for i in sel:
        ang = math.radians(wd[i]); sp = ws[i] * 1.944
        ax.barbs([41], [y[i]], [-sp * math.sin(ang)], [-sp * math.cos(ang)], length=5, linewidth=0.7, color="0.2", zorder=6, clip_on=False)
    ax.set_title(title, fontsize=9)
    ax.text(0.02, 0.02, note,
            transform=ax.transAxes, fontsize=7, va="bottom", bbox=dict(fc="white", ec="0.7", alpha=0.9))
    ax.legend(fontsize=7, loc="upper right")


def fig_skewt(case: Case, out: Path, stations, time, plt):
    snd = case.snd; ids = stations or list(snd)[:3]
    picks = [(w, C.ascent_at(snd[w], time) if time else max(snd[w]["soundings"], key=lambda a: a["time"])) for w in ids if w in snd]
    picks = [(w, a) for w, a in picks if a]
    if not picks:
        return
    fig, axs = plt.subplots(1, len(picks), figsize=(4.7 * len(picks), 5.6), squeeze=False)
    for ax, (w, a) in zip(axs[0], picks):
        s = snd[w]
        skewt(ax, a, f"{s.get('name') or w} ({s['lat']:.1f}°N {s['lon']:.1f}°E)\n{case.local(C.norm_time(a['time']), '%Y-%m-%d %H')}", case.T)
    fig.tight_layout(); fig.savefig(out / "fig_skewt.png"); plt.close(fig)


def fig_nowcast(case: Case, out: Path, marks, plt):
    from matplotlib.colors import BoundaryNorm, ListedColormap
    T = case.T; nc = case.now; meta = case.meta["nowcast"]; panels = [k for k in ("sat", "radar") if k in nc.files]
    if not panels:
        return
    fig, axs = plt.subplots(1, len(panels), figsize=(6.5 * len(panels), 5.6), squeeze=False)
    for ax, k in zip(axs[0], panels):
        a = nc[k]; lat = nc[f"{k}:lat"]; lon = nc[f"{k}:lon"]; m = meta[k]
        box = (float(lat.min()), float(lat.max()), float(lon.min()), float(lon.max()))
        setmap(ax, box, case, T[k].format(src=m["source"], var=m["variable"], t=m["time"][:16], loc=case.local(m["time"])), marks)
        step = max(1, a.shape[0] // 800)
        if k == "sat":
            im = ax.pcolormesh(lon[::step], lat[::step], a[::step, ::step], cmap="gray_r", vmin=190, vmax=300, shading="auto", zorder=0)
            ax.contour(lon[::step], lat[::step], a[::step, ::step], levels=[210, 230], colors=["#8e0d8e", "#1f5fbf"], linewidths=0.6, zorder=3)
            if case.storm:
                for ag, v in case.storm.get("agencies", {}).items():
                    p0 = v["points"][0]; ax.plot(p0["lon"], p0["lat"], "x", ms=9, mew=2, color="#c0392b", zorder=10)
                    ax.text(p0["lon"] + 0.5, p0["lat"] + 0.2, T["centre"].format(id=case.storm["id"], ag=ag.upper(), t=p0["time"][5:13] + "Z"), fontsize=7, color="#c0392b", zorder=10); break
            fig.colorbar(im, ax=ax, shrink=0.75, label="K")
        else:
            cm = ListedColormap(RAIN_COLORS); norm = BoundaryNorm(RAIN_LEVELS, cm.N)
            aa = np.where(a[::step, ::step] <= 0.1, np.nan, a[::step, ::step])
            if m["variable"] == "cref":
                im = ax.pcolormesh(lon[::step], lat[::step], np.where(a[::step, ::step] < 5, np.nan, a[::step, ::step]), cmap="turbo", vmin=5, vmax=70, shading="auto", zorder=2); fig.colorbar(im, ax=ax, shrink=0.75, label="dBZ")
            else:
                im = ax.pcolormesh(lon[::step], lat[::step], aa, cmap=cm, norm=norm, shading="auto", zorder=2); fig.colorbar(im, ax=ax, shrink=0.75, label="mm/h")
    fig.tight_layout(); fig.savefig(out / "fig_nowcast.png"); plt.close(fig)


FIRE_LABELS = {
    "zh": ("FIRMS 火点逐日推进（颜色 = UTC 日期，大小 = FRP）", "每小时火点 FRP 合计（MW）", "火点数"),
    "zh-Hant": ("FIRMS 火點逐日推進（顏色 = UTC 日期，大小 = FRP）", "每小時火點 FRP 合計（MW）", "火點數"),
    "ja": ("FIRMS 火災検知の日別推移（色 = UTC 日付、大きさ = FRP）", "1 時間ごとの FRP 合計（MW）", "検知数"),
    "en": ("FIRMS active fires by day (colour = UTC day, size = FRP)", "FRP summed per hour (MW)", "detections"),
}


def fig_fires(case: Case, out: Path, box, marks, plt):
    """Where it burned, day by day, and how hard, hour by hour: the fire
    product's detections in the box (NASA FIRMS, VIIRS and MODIS)."""
    title, frp_label, count_label = FIRE_LABELS.get(case.lang, FIRE_LABELS["en"])
    fs = case.fires
    days = sorted({f["time"][:10] for f in fs})
    cmap = plt.get_cmap("plasma", max(1, len(days)))
    fig, axs = plt.subplots(1, 2, figsize=(13, 5.2), gridspec_kw={"width_ratios": [1.1, 1]})
    if box is None:
        lats = [f["lat"] for f in fs]; lons = [f["lon"] for f in fs]
        box = (min(lats) - 0.5, max(lats) + 0.5, min(lons) - 0.5, max(lons) + 0.5)
    setmap(axs[0], box, case, title, marks)
    for i, day in enumerate(days):
        group = [f for f in fs if f["time"].startswith(day)]
        axs[0].scatter([f["lon"] for f in group], [f["lat"] for f in group], s=[4 + min(f["frp"], 200) / 4 for f in group],
                       color=cmap(i), edgecolors="none", alpha=0.8, label=day[5:], zorder=6)
    axs[0].legend(fontsize=7, loc="best", markerscale=1.5)
    hours: dict[str, list[float]] = {}
    for f in fs:
        hours.setdefault(f["time"][:13], []).append(f["frp"])
    ts = [C.parse_time(h + ":00Z").astimezone(case.tz) for h in sorted(hours)]
    axs[1].bar(ts, [sum(hours[h]) for h in sorted(hours)], width=1 / 24, color="#c0392b")
    axs[1].set_ylabel(frp_label)
    twin = axs[1].twinx(); twin.plot(ts, [len(hours[h]) for h in sorted(hours)], ".", color="0.3", ms=4); twin.set_ylabel(count_label)
    axs[1].set_xlabel(f"UTC{case.tzh:+g}" if case.tzh else "UTC")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out / "fig_fires.png"); plt.close(fig)


def fig_ensemble(case: Case, out: Path, name, t_from, t_to, mark, plt):
    T = case.T; lat, lon = case.meta["point"]
    res = C.ensemble_spread(case.ens, lat, lon, [], t_from, t_to, box=case.meta.get("box"))
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.2))
    hours = []
    for k, r in res.items():
        ds = [c["dist_km"] for c in r["closest"]]; ts = [C.parse_time(c["time"]).astimezone(case.tz) for c in r["closest"]]
        if not ds:
            continue
        lab = f"{ENS_NAMES.get(k, k)} {r['run']} (n={len(ds)}/{r['members']})"
        axs[0].hist(ds, bins=np.arange(0, max(ds) + 50, 50), alpha=0.5, color=MODEL_COLORS.get(k.replace("ens", "").replace("gefs", "gfs"), "0.5"), label=lab)
        h = [t.timestamp() / 3600 for t in ts]; hours += h
        axs[1].hist(h, bins=np.arange(math.floor(min(h) / 6) * 6, max(h) + 6, 6), alpha=0.5, color=MODEL_COLORS.get(k.replace("ens", "").replace("gefs", "gfs"), "0.5"), label=lab)
    axs[0].set_xlabel(T["ens_d"].format(name=name)); axs[0].set_ylabel(T["ens_n"]); axs[0].legend(fontsize=7); axs[0].grid(alpha=0.4, lw=0.3)
    axs[1].set_xlabel(T["ens_t"].format(tz=case.tzh)); axs[1].legend(fontsize=7); axs[1].grid(alpha=0.4, lw=0.3)
    if hours:
        ticks = np.arange(math.floor(min(hours) / 6) * 6, max(hours) + 6, 6)
        axs[1].set_xticks(ticks); axs[1].set_xticklabels([datetime.fromtimestamp(h * 3600, tz=timezone.utc).astimezone(case.tz).strftime("%d/%H") for h in ticks], rotation=45, ha="right", fontsize=7)
    if mark:
        axs[1].axvline(C.parse_time(mark).timestamp() / 3600, color="k", ls="-.", lw=1)
    fig.suptitle(T["ens_title"].format(name=name), fontsize=9)
    fig.tight_layout(); fig.savefig(out / "fig_ensemble.png"); plt.close(fig)


def section_tag(case: Case, k: int) -> str:
    """'' with one section, else its number: the A/B ends then read A1/B1, A2/B2."""
    return "" if len(case.sections) == 1 else str(k + 1)


def section_lines(ax, case: Case):
    """Each fetched section's line on a map, A and B at its ends."""
    specs = (case.meta.get("columns") or {}).get("sections", [])
    for k, (spec, sec) in enumerate(zip(specs, case.sections)):
        n = section_tag(case, k)
        lon = np.where(sec["lon"] < 0, sec["lon"] + 360, sec["lon"]) if ax.get_xlim()[1] > 180 else sec["lon"]
        ax.plot(lon, sec["lat"], color="#d35400", lw=1.6, zorder=11)
        ax.text(lon[0], sec["lat"][0], f" A{n}", color="#d35400", fontsize=8, fontweight="bold", zorder=11)
        ax.text(lon[-1], sec["lat"][-1], f" B{n}", color="#d35400", fontsize=8, fontweight="bold", zorder=11)


def _pressure_axis(ax):
    ax.set_yscale("log"); ax.set_ylim(1050, 100); ax.set_yticks(PRESSURE_TICKS); ax.set_yticklabels(PRESSURE_TICKS, fontsize=7)
    ax.minorticks_off(); ax.set_ylabel("hPa", fontsize=8)


def fig_section(case: Case, out: Path, fill: str, plt):
    """One file per section, one panel per time: `fill` shaded, θ contours,
    the wind across the section as isotachs, 0 °C, the model's ground."""
    CT = case.CT; cmap, step = SECTION_FILLS[fill]; run = case.meta["columns"]["run"]
    run_label = f"{MODEL_NAMES.get(run.split('.')[0], run)} {run.split('.')[1][:8]}/{run.split('.')[1][8:10]}Z"
    for k, (spec, sec) in enumerate(zip(case.meta["columns"]["sections"], case.sections)):
        nt = len(sec["time"]); d = sec["dist"]; p = sec["p"]
        values = {"thetae": sec["thetae"], "theta": sec["theta"], "rh": sec["rh"], "wind": sec["speed"]}[fill]
        finite = values[np.isfinite(values) & (p[None, :, None] >= 200)] if fill in ("theta", "thetae") else values[np.isfinite(values)]
        lo, hi = (0, 100) if fill == "rh" else (math.floor(np.percentile(finite, 1) / step) * step, math.ceil(np.percentile(finite, 99) / step) * step)
        levels = np.arange(lo, hi + step, step)
        fig, axs = plt.subplots(nt, 1, figsize=(10, 4.2 * nt), squeeze=False)
        for i in range(nt):
            ax = axs[i, 0]; T = str(sec["time"][i])[:16] + "Z"
            cf = ax.contourf(d, p, values[i], levels=levels, cmap=cmap, extend="both" if fill != "rh" else "neither")
            cs = ax.contour(d, p, sec["theta"][i], levels=np.arange(240, 480, 4), colors="0.35", linewidths=0.5)
            ax.clabel(cs, cs.levels[::2], fmt="%d", fontsize=6)
            across = sec["across"][i]
            for lv, ls in ((np.arange(20, 120, 10), "-"), (-np.arange(20, 120, 10)[::-1], "--")):
                c = ax.contour(d, p, across, levels=lv, colors="#6c1d9c", linewidths=[1.4 if abs(x) >= 30 else 0.8 for x in lv], linestyles=ls)
                ax.clabel(c, fmt="%d", fontsize=6)
            ax.contour(d, p, sec["tmp"][i], levels=[0], colors="#1f5fbf", linewidths=1.3)
            ps = sec["psfc"][i]
            ax.fill_between(d, np.where(np.isfinite(ps), ps, 1050), 1050, color="#7a5c3a", alpha=0.85, zorder=8, lw=0, label=CT["ground"])
            _pressure_axis(ax); ax.set_xlim(d[0], d[-1])
            ax.set_title(CT["sec"].format(run=run_label, name=spec["name"], t=case.local(T)), fontsize=9)
            ticks = np.linspace(0, d[-1], 6)
            ax.set_xticks(ticks)
            ax.set_xticklabels([f"{x:.0f}\n{np.interp(x, d, sec['lat']):.1f}°, {np.interp(x, d, sec['lon']):.1f}°" for x in ticks], fontsize=7)
            ax.text(0, 1.01, "A" + section_tag(case, k), transform=ax.transAxes, fontsize=9, fontweight="bold", ha="left", va="bottom")
            ax.text(1, 1.01, "B" + section_tag(case, k), transform=ax.transAxes, fontsize=9, fontweight="bold", ha="right", va="bottom")
            fig.colorbar(cf, ax=ax, pad=0.01, label=CT["fill"][fill])
        axs[-1, 0].set_xlabel(CT["dist"], fontsize=8)
        fig.text(0.01, 0.003, CT["sec_note"], fontsize=7, color="0.3")
        fig.tight_layout(rect=(0, 0.02, 1, 1)); fig.savefig(out / f"fig_section{'' if k == 0 else k + 1}.png"); plt.close(fig)


def fig_timeheight(case: Case, out: Path, name, hours, mark, plt):
    """The point's column through the run: RH shaded, temperature contoured
    (0 °C bold), wind barbs every 6 h, the model's surface pressure."""
    import matplotlib.dates as mdates
    CT = case.CT; col = case.column; run = case.meta["columns"]["run"]
    run_label = f"{MODEL_NAMES.get(run.split('.')[0], run)} {run.split('.')[1][:8]}/{run.split('.')[1][8:10]}Z"
    t = col["time"]; keep = (t - t[0]) <= np.timedelta64(hours, "h")
    t = t[keep]; p = col["p"]
    times = [C.parse_time(str(x)[:16] + "Z").astimezone(case.tz) for x in t]
    below = p[None, :] > (col["psfc"][keep][:, None] - XC.GROUND_CLEARANCE_HPA)
    rh = np.where(below, np.nan, col["rh"][keep]); tmp = np.where(below, np.nan, col["tmp"][keep])
    fig, ax = plt.subplots(figsize=(11, 5.2))
    cf = ax.contourf(times, p, rh.T, levels=[50, 60, 70, 80, 90, 95, 101], cmap="Greens", extend="neither")
    cs = ax.contour(times, p, tmp.T, levels=np.arange(-80, 45, 5), colors="#c0392b", linewidths=0.6)
    ax.clabel(cs, fmt="%d", fontsize=6)
    ax.contour(times, p, tmp.T, levels=[0], colors="#1f5fbf", linewidths=1.5)
    hrs = ((t - t[0]) / np.timedelta64(1, "h")).astype(int)
    step = 6 if hours <= 168 else 12
    sel = [i for i in range(len(t)) if hrs[i] % step == 0]
    P = np.array(PRESSURE_TICKS[:-1], float)
    for i in sel:
        for P0 in P:
            if P0 not in p:
                continue
            j = int(np.where(p == P0)[0][0])
            if below[i, j]:
                continue
            u, v = col["ugrd"][keep][i, j], col["vgrd"][keep][i, j]
            ax.barbs(mdates.date2num(times[i]), P0, u * 1.944, v * 1.944, length=5, linewidth=0.5, color="0.15", zorder=7)
    ax.fill_between(times, col["psfc"][keep], 1050, color="#7a5c3a", alpha=0.85, zorder=8, lw=0)
    _pressure_axis(ax)
    if mark:
        ax.axvline(C.parse_time(mark).astimezone(case.tz), color="k", lw=1.0, ls="-.", zorder=9)
    lat, lon = col["cell"]
    ax.set_title(CT["th"].format(run=run_label, name=name, lat=lat, lon=lon), fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%H", tz=case.tz))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12], tz=case.tz) if hours <= 168 else mdates.DayLocator(tz=case.tz))
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=7); ax.set_xlabel(CT["time"].format(tz=case.tzh), fontsize=8)
    fig.colorbar(cf, ax=ax, pad=0.01, label="RH %")
    fig.tight_layout(); fig.savefig(out / "fig_timeheight.png"); plt.close(fig)


def fig_modelskewt(case: Case, out: Path, name, times, plt):
    """The column store's model sounding at the point, one panel per time,
    with the indices `xue_columns.profile_at` computes."""
    CT = case.CT; col = case.column; run = case.meta["columns"]["run"]
    run_label = f"{MODEL_NAMES.get(run.split('.')[0], run)} {run.split('.')[1][:8]}/{run.split('.')[1][8:10]}Z"
    times = times[:4]
    fig, axs = plt.subplots(1, len(times), figsize=(4.7 * len(times), 5.6), squeeze=False)
    f = lambda x, s="{:.0f}": "-" if not np.isfinite(x) else s.format(x)  # noqa: E731
    for ax, T in zip(axs[0], times):
        pr = XC.profile_at(col, T)
        note = CT["idx"].format(pw=f(pr["pw"], "{:.1f}"), fl=f(pr["freezing"]), lcl=f(pr["lcl"]), k=f(pr["k"]), sh=f(pr["shear06"]), cape=f(pr["cape"]))
        note = note.replace(" · LCL", "\nLCL")
        lat, lon = col["cell"]
        skewt_arrays(ax, pr["p"], pr["t"], pr["td"], pr["wd"], pr["ws"],
                     CT["mskewt"].format(run=run_label, name=name, lat=lat, lon=lon, t="\n" + case.local(pr["time"], "%Y-%m-%d %H")), case.T, note, mandatory_barbs=True)
    fig.tight_layout(); fig.savefig(out / "fig_modelskewt.png"); plt.close(fig)


# --- CLI ------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir"); ap.add_argument("--out"); ap.add_argument("--tz", type=float, default=0); ap.add_argument("--lang", default="zh", choices=list(L))
    ap.add_argument("--figures", default="all"); ap.add_argument("--map-times", help="two valid times for the map figures (default: first and last of the case)")
    ap.add_argument("--map-box", type=float, nargs=4, metavar=("S", "N", "W", "E"), help="map extent (default: the case box)")
    ap.add_argument("--point-name", default="point"); ap.add_argument("--mark", help="a valid time to mark on the meteogram and ensemble timing")
    ap.add_argument("--window", help="T1,T2 shaded on the meteogram"); ap.add_argument("--span", help="T1,T2 x-limits of the meteogram (default: the whole axis)"); ap.add_argument("--skewt", help="WMO ids for the skew-T panel"); ap.add_argument("--skewt-time")
    ap.add_argument("--borders", help="ISO 3166-1 alpha-3 codes whose outlines to draw, e.g. MNG,CHN")
    ap.add_argument("--section-fill", default="thetae", choices=list(SECTION_FILLS), help="what the section figure shades")
    ap.add_argument("--th-hours", type=int, default=120, help="hours of the run the time-height figure spans")
    ap.add_argument("--ens-from"); ap.add_argument("--ens-to"); ap.add_argument("--font", default="Hiragino Sans GB,Noto Sans CJK SC,Arial Unicode MS,DejaVu Sans")
    a = ap.parse_args(argv)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = a.font.split(","); plt.rcParams["axes.unicode_minus"] = False; plt.rcParams["figure.dpi"] = 130
    import logging; logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    case = Case(Path(a.dir), a.tz, a.lang, tuple(a.borders.split(",")) if a.borders else ())
    out = Path(a.out) if a.out else Path(a.dir) / "figures"; out.mkdir(parents=True, exist_ok=True)
    box = tuple(a.map_box) if a.map_box else tuple(case.meta["box"]) if case.meta.get("box") else None
    marks = [tuple(case.meta["point"])] if case.meta.get("point") else []
    times = [C.norm_time(t) for t in a.map_times.split(",")] if a.map_times else ([case.meta["times"][0], case.meta["times"][-1]] if len(case.meta.get("times", [])) > 1 else case.meta.get("times", []))
    want = set(a.figures.split(",")) if a.figures != "all" else {"tracks", "upper", "verify", "surface", "moisture", "meteogram", "skewt", "nowcast", "ensemble", "fires", "section", "timeheight", "modelskewt"}
    window = a.window.split(",") if a.window else None
    done = []
    if "tracks" in want and case.storm and box:
        fig_tracks(case, out, box, marks, plt); done.append("tracks")
    if "upper" in want and case.fields is not None and times:
        fig_upper(case, out, box, marks, times, plt); done.append("upper")
    if "verify" in want and case.fields is not None and case.snd:
        fig_verify(case, out, box, marks, plt); done.append("verify")
    if "surface" in want and case.fields is not None and times:
        fig_surface(case, out, box, marks, times, plt); done.append("surface")
    if "moisture" in want and case.fields is not None and times:
        fig_moisture(case, out, box, marks, times, plt); done.append("moisture")
    if "meteogram" in want and case.point is not None:
        fig_meteogram(case, out, a.point_name, a.mark, window, a.span.split(",") if a.span else None, plt); done.append("meteogram")
    if "skewt" in want and case.snd:
        fig_skewt(case, out, a.skewt.split(",") if a.skewt else None, a.skewt_time, plt); done.append("skewt")
    if "nowcast" in want and case.now is not None:
        fig_nowcast(case, out, marks, plt); done.append("nowcast")
    if "fires" in want and case.fires:
        fig_fires(case, out, box, marks, plt); done.append("fires")
    if "ensemble" in want and case.ens and case.meta.get("point"):
        fig_ensemble(case, out, a.point_name, a.ens_from, a.ens_to, a.mark, plt); done.append("ensemble")
    if "section" in want and case.sections:
        fig_section(case, out, a.section_fill, plt); done.append("section")
    if "timeheight" in want and case.column:
        fig_timeheight(case, out, a.point_name, a.th_hours, a.mark, plt); done.append("timeheight")
    if "modelskewt" in want and case.column and (a.skewt_time or times):
        fig_modelskewt(case, out, a.point_name, [a.skewt_time] if a.skewt_time else times, plt); done.append("modelskewt")
    print(f"wrote {out}: {', '.join(done)}")


if __name__ == "__main__":
    main()
