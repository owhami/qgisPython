import json
import urllib.request
import time
from qgis.core import (
    QgsProject, QgsFeature, QgsGeometry, QgsVectorLayer,
    QgsDistanceArea, QgsField, QgsCoordinateTransform, 
    QgsCoordinateReferenceSystem, QgsPointXY,
    QgsSimpleLineSymbolLayer, QgsLineSymbol, QgsSingleSymbolRenderer
)
from qgis.utils import iface
from PyQt5.QtWidgets import QApplication, QInputDialog, QMessageBox
from PyQt5.QtCore import QVariant

def run_routing_script_with_search():
    # 1. Nama layer
    user_layer_name = 'tbUser'
    fat_layer_name = 'tbFAT'

    user_layers = QgsProject.instance().mapLayersByName(user_layer_name)
    fat_layers = QgsProject.instance().mapLayersByName(fat_layer_name)

    if not user_layers or not fat_layers:
        print("Error: Layer tbUser atau tbFAT tidak ditemukan!")
        return

    user_layer = user_layers[0]
    fat_layer = fat_layers[0]

    # 2. Daftar userPaniki
    daftar_user = []
    user_dict = {} 
    
    for f in user_layer.getFeatures():
        if not f.hasGeometry() or f.geometry().isNull():
            continue
        
        raw_name = f['userPaniki'] if 'userPaniki' in f.fields().names() else f.id()
        nama_user = str(raw_name).strip() if raw_name else str(f.id())
        
        if nama_user not in daftar_user:
            daftar_user.append(nama_user)
            user_dict[nama_user] = f

    if not daftar_user:
        print("Tidak ada data user valid di layer tbUser.")
        return

    # 3. Pop-up Search User
    parent = iface.mainWindow() if iface else None
    search_text, ok = QInputDialog.getText(
        parent, 
        "Cari User PANIKI", 
        "Ketik userPaniki (atau sebagian namanya):"
    )

    if not ok or not search_text.strip():
        print("Pencarian dibatalkan.")
        return

    # 4. Logika Filter Teks
    search_query = search_text.strip().lower()
    matched_users = [name for name in daftar_user if search_query in name.lower()]

    if len(matched_users) == 0:
        QMessageBox.warning(parent, "Pencarian Gagal", f"Tidak menemukan user yang mengandung kata '{search_text}'.")
        return
    elif len(matched_users) == 1:
        selected_user = matched_users[0]
    else:
        matched_users.sort()
        selected_user, ok_combo = QInputDialog.getItem(
            parent, "Pilih User Spesifik", f"Ditemukan {len(matched_users)} nama yang mirip:", matched_users, 0, False
        )
        if not ok_combo or not selected_user:
            return

    print(f"\n--- MEMULAI PENCARIAN UNTUK: {selected_user} ---")

    # 5. Transformasi CRS ke EPSG:4326
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform_to_wgs84 = QgsCoordinateTransform(user_layer.crs(), crs_wgs84, QgsProject.instance())
    transform_to_local = QgsCoordinateTransform(crs_wgs84, user_layer.crs(), QgsProject.instance())

    d_wgs = QgsDistanceArea()
    d_wgs.setSourceCrs(crs_wgs84, QgsProject.instance().transformContext())
    d_wgs.setEllipsoid('WGS84')

    # 6. Ekstrak data User
    target_user_feat = user_dict[selected_user]
    user_pt_local = target_user_feat.geometry().asPoint()
    
    user_geom_wgs = QgsGeometry(target_user_feat.geometry())
    user_geom_wgs.transform(transform_to_wgs84)
    user_pt_wgs = user_geom_wgs.asPoint()

    # 7. Cache data FAT
    fat_data = []
    for f in fat_layer.getFeatures():
        if not f.hasGeometry():
            continue
            
        try:
            idle_val = int(f['usedSPLT'])
        except (ValueError, TypeError, KeyError):
            idle_val = 8
            
        if idle_val >= 8:
            continue
        
        fat_pt_local = f.geometry().asPoint()
        
        fat_geom_wgs = QgsGeometry(f.geometry())
        fat_geom_wgs.transform(transform_to_wgs84)
        fat_pt_wgs = fat_geom_wgs.asPoint()
        
        idFAT = f['idFAT'] if 'idFAT' in f.fields().names() else str(f.id())
        nama_olt = str(f['idOLT']) if 'idOLT' in f.fields().names() else "-"
        koordinat_teks = f"{fat_pt_wgs.y():.6f}, {fat_pt_wgs.x():.6f}"
        
        fat_data.append({
            'name': idFAT, 
            'point_wgs': fat_pt_wgs,
            'point_local': fat_pt_local,
            'idle': idle_val,
            'olt': nama_olt,
            'koordinat': koordinat_teks
        })

    # 8. Layer Output
    layer_name = f"Rute_{selected_user}_OSRM"
    line_layer = QgsVectorLayer(f"LineString?crs={user_layer.crs().authid()}", layer_name, "memory")
    provider = line_layer.dataProvider()
    
    provider.addAttributes([
        QgsField("userPaniki", QVariant.String),
        QgsField("idFAT", QVariant.String),
        QgsField("usedSPLT", QVariant.Int),
        QgsField("idOLT", QVariant.String),
        QgsField("koordinatFAT", QVariant.String),
        QgsField("jarak_jalan_m", QVariant.Double)
    ])
    line_layer.updateFields()

    new_features = []

    # Helper: satu kali percobaan request ke server FOSSGIS untuk profil tertentu
    # ('foot' atau 'car'). Mengembalikan (distance, coords) kalau valid, atau
    # (None, None) kalau gagal/error/tidak valid. Semua error ditangani di
    # dalam sini supaya percobaan profil lain tetap bisa dilanjutkan.
    def query_osrm(profile, lon_a, lat_a, lon_b, lat_b, straight_dist):
        url = f"https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/{lon_a},{lat_a};{lon_b},{lat_b}?overview=full&geometries=geojson"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'QGIS-PyQGIS-Script'})
            res = urllib.request.urlopen(req)
            data = json.loads(res.read().decode('utf-8'))
        except Exception as e:
            print(f"[{profile} ERROR: {repr(e)}] ", end="")
            return None, None

        if data.get('code') != 'Ok':
            print(f"[{profile} code={data.get('code')}] ", end="")
            return None, None

        dist = data['routes'][0]['distance']
        coords = data['routes'][0]['geometry']['coordinates']

        # OSRM kadang membalas code='Ok' dengan distance mendekati 0 meski
        # titik asal & tujuan jelas berjauhan -- indikasi start & end
        # ke-snap ke node/ruas yang sama. Anggap tidak valid.
        if dist < 5 and straight_dist > 10:
            print(f"[{profile} snap ke titik sama, distance={dist}m -> diabaikan] ", end="")
            return None, None

        if dist > 500:
            print(f"[{profile}={round(dist,2)}m, >500m] ", end="")
            return None, None

        return dist, coords

    # 9. Request API OSRM (FOSSGIS) -- coba profil 'foot' DAN 'car', lalu pilih
    # yang hasilnya paling PENDEK & valid di antara keduanya (bukan cuma
    # berhenti begitu salah satu berhasil). Ini penting karena kadang profil
    # 'foot' "berhasil" tapi dengan rute memutar jauh -- padahal jalan yang
    # sama mungkin ramah untuk profil 'car' dan lebih pendek/masuk akal.
    # TIDAK ADA fallback tarik garis lurus sama sekali -- kalau kedua profil
    # gagal menemukan rute nyata, FAT tersebut dilewati saja, supaya tidak
    # pernah muncul garis yang menyeberangi sungai/area yang tidak valid.
    for fat in fat_data:
        straight_dist = d_wgs.measureLine(user_pt_wgs, fat['point_wgs'])
        
        if straight_dist > 500:
            continue

        print(f"Menguji FAT: {fat['name']} [Sisa Port: {fat['idle']}] (Jarak Lurus: {round(straight_dist, 2)}m) ... ", end="")

        lon1, lat1 = user_pt_wgs.x(), user_pt_wgs.y()
        lon2, lat2 = fat['point_wgs'].x(), fat['point_wgs'].y()

        kandidat = []  # list of (distance, coords, label) -- kumpulkan semua hasil valid

        # A. Coba FOOT maju dan CAR maju -- keduanya, bukan salah satu saja
        d_foot, c_foot = query_osrm('foot', lon1, lat1, lon2, lat2, straight_dist)
        if c_foot:
            kandidat.append((d_foot, c_foot, 'foot'))

        time.sleep(1.0)
        d_car, c_car = query_osrm('car', lon1, lat1, lon2, lat2, straight_dist)
        if c_car:
            kandidat.append((d_car, c_car, 'car'))

        # B. Kalau DUA-DUANYA gagal maju, baru coba arah mundur untuk keduanya
        if not kandidat:
            time.sleep(1.0)
            d_bw, c_bw = query_osrm('foot', lon2, lat2, lon1, lat1, straight_dist)
            if c_bw:
                kandidat.append((d_bw, c_bw, 'foot (mundur)'))

            time.sleep(1.0)
            d_car_bw, c_car_bw = query_osrm('car', lon2, lat2, lon1, lat1, straight_dist)
            if c_car_bw:
                kandidat.append((d_car_bw, c_car_bw, 'car (mundur)'))

        # C. Kalau ada kandidat valid, pilih yang jaraknya PALING PENDEK
        if kandidat:
            route_dist, route_coords, profile_used = min(kandidat, key=lambda k: k[0])
            print(f"BERHASIL! [{profile_used}] Rute Jalan = {round(route_dist, 2)}m")
            points = [QgsPointXY(pt[0], pt[1]) for pt in route_coords]
            route_geom = QgsGeometry.fromPolylineXY(points)
            route_geom.transform(transform_to_local)
            
            new_feat = QgsFeature(line_layer.fields())
            new_feat.setGeometry(route_geom)
            new_feat.setAttributes([str(selected_user), str(fat['name']), int(fat['idle']), str(fat['olt']), str(fat['koordinat']), float(round(route_dist, 2))])
            new_features.append(new_feat)
        else:
            # TIDAK ADA fallback tarik lurus -- FAT ini dilewati saja
            print("GAGAL (tidak ada rute jalan/kendaraan yang valid -- dilewati, TIDAK ditarik lurus)")
        
        # Jeda 1 detik agar sesuai kebijakan rate-limit server (maks 1 request/detik)
        time.sleep(1.0)

    # 10. Tampilkan Hasil
    if new_features:
        line_layer.startEditing()
        line_layer.addFeatures(new_features)
        line_layer.commitChanges()
        line_layer.updateExtents()
        
        symbol_layer = QgsSimpleLineSymbolLayer.create({
            'line_width': '0.8',          
            'line_color': '255,0,0,255'   
        })
        
        symbol = QgsLineSymbol([symbol_layer])
        renderer = QgsSingleSymbolRenderer(symbol)
        line_layer.setRenderer(renderer)
        
        QgsProject.instance().addMapLayer(line_layer)
        
        iface.mapCanvas().setExtent(line_layer.extent())
        iface.mapCanvas().refresh()
        
        QMessageBox.information(parent, "Sukses", f"Ditemukan {len(new_features)} jalur rute valid (mengikuti jalan/jembatan nyata) yang port-nya tersedia.")
    else:
        iface.mapCanvas().setCenter(target_user_feat.geometry().asPoint())
        iface.mapCanvas().zoomScale(2000) 
        iface.mapCanvas().refresh()
        
        QMessageBox.information(parent, "Selesai", "Tidak ada rute jalan/kendaraan yang valid ditemukan dalam radius 500m.")
    
    print("--- SELESAI ---")

run_routing_script_with_search()
