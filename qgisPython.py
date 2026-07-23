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
from PyQt5.QtWidgets import QInputDialog, QMessageBox
from PyQt5.QtCore import QVariant

def run_routing_script_with_search():
    # 1. Nama layer
    user_layer_name = 'tbUsrPan'
    fat_layer_name = 'tbFAT'

    user_layers = QgsProject.instance().mapLayersByName(user_layer_name)
    fat_layers = QgsProject.instance().mapLayersByName(fat_layer_name)

    if not user_layers or not fat_layers:
        print("Error: Layer tbUsrPan atau tbFAT tidak ditemukan!")
        return

    user_layer = user_layers[0]
    fat_layer = fat_layers[0]

    # 2. Daftar userPaniki
    daftar_user = []
    user_dict = {} 
    
    for f in user_layer.getFeatures():
        if not f.hasGeometry():
            continue
        
        nama_user = f['userPaniki'] if 'userPaniki' in f.fields().names() else str(f.id())
        
        if nama_user not in daftar_user:
            daftar_user.append(nama_user)
            user_dict[nama_user] = f

    if not daftar_user:
        print("Tidak ada data user valid di layer tbUsrPan.")
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
    user_geom_wgs = QgsGeometry(target_user_feat.geometry())
    user_geom_wgs.transform(transform_to_wgs84)
    user_pt_wgs = user_geom_wgs.asPoint()

    # 7. Cache data FAT, CEK IDLESPLITTER, dan NAMA OLT
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
        
        fat_geom_wgs = QgsGeometry(f.geometry())
        fat_geom_wgs.transform(transform_to_wgs84)
        fat_pt_wgs = fat_geom_wgs.asPoint()
        
        idFAT = f['idFAT'] if 'idFAT' in f.fields().names() else str(f.id())
        
        # --- PERBAIKAN: MENGAMBIL NAMA OLT DARI LAYER FAT ---
        nama_olt = str(f['idOLT']) if 'idOLT' in f.fields().names() else "-"
        
        fat_data.append({
            'name': idFAT, 
            'point_wgs': fat_pt_wgs,
            'idle': idle_val,
            'olt': nama_olt  # Simpan ke cache memori
        })

    # 8. Layer Output
    layer_name = f"Rute_{selected_user}_OSRM"
    line_layer = QgsVectorLayer(f"LineString?crs={user_layer.crs().authid()}", layer_name, "memory")
    provider = line_layer.dataProvider()
    
    # --- PERBAIKAN: TAMBAH FIELD NAMA OLT ---
    provider.addAttributes([
        QgsField("userPaniki", QVariant.String),
        QgsField("idFAT", QVariant.String),
        QgsField("usedSPLT", QVariant.Int),
        QgsField("idOLT", QVariant.String), # Kolom baru untuk OLT
        QgsField("jarak_jalan_m", QVariant.Double)
    ])
    line_layer.updateFields()

    new_features = []

    # 9. Request API OSRM
    for fat in fat_data:
        straight_dist = d_wgs.measureLine(user_pt_wgs, fat['point_wgs'])
        
        if straight_dist > 500:
            continue

        print(f"Menguji FAT: {fat['name']} [Sisa Port: {fat['idle']}] (Jarak Lurus: {round(straight_dist, 2)}m) ... ", end="")

        lon1, lat1 = user_pt_wgs.x(), user_pt_wgs.y()
        lon2, lat2 = fat['point_wgs'].x(), fat['point_wgs'].y()
        
        url = f"http://router.project-osrm.org/route/v1/foot/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'QGIS-PyQGIS-Script-Foot'})
            response = urllib.request.urlopen(req)
            data = json.loads(response.read().decode('utf-8'))
            
            if data['code'] == 'Ok':
                route_dist = data['routes'][0]['distance']
                
                if route_dist <= 500:
                    print(f"BERHASIL! Rute Jalan = {round(route_dist, 2)}m")
                    
                    coords = data['routes'][0]['geometry']['coordinates']
                    points = [QgsPointXY(pt[0], pt[1]) for pt in coords]
                    route_geom = QgsGeometry.fromPolylineXY(points)
                    route_geom.transform(transform_to_local)
                    
                    new_feat = QgsFeature()
                    new_feat.setGeometry(route_geom)
                    
                    # --- PERBAIKAN: MASUKKAN NILAI OLT KE TABEL ATRIBUT ---
                    # Urutan di sini harus persis sama dengan urutan di langkah 8
                    new_feat.setAttributes([selected_user, fat['name'], fat['idle'], fat['olt'], round(route_dist, 2)])
                    new_features.append(new_feat)
                else:
                    print(f"GAGAL (Rute jalan terlalu jauh: {round(route_dist, 2)}m)")
            else:
                 print(f"GAGAL API (Respon: {data['code']})")
                    
        except Exception as e:
            print(f"ERROR API/JARINGAN ({e})")
        
        time.sleep(0.2) 

 # 10. Tampilkan Hasil
    if new_features:
        provider.addFeatures(new_features)
        
        # Style Garis Merah 0.8 mm
        symbol_layer = QgsSimpleLineSymbolLayer.create({
            'line_width': '0.8',          
            'line_color': '255,0,0,255'   
        })
        
        symbol = QgsLineSymbol([symbol_layer])
        renderer = QgsSingleSymbolRenderer(symbol)
        line_layer.setRenderer(renderer)
        
        QgsProject.instance().addMapLayer(line_layer)
        
        # --- FITUR BARU: AUTO ZOOM KE HASIL RUTE ---
        iface.mapCanvas().setExtent(line_layer.extent())
        iface.mapCanvas().refresh()
        # ------------------------------------------
        
        QMessageBox.information(parent, "Sukses", f"Ditemukan {len(new_features)} jalur rute valid yang port-nya tersedia.")
    else:
        # --- FITUR BARU: AUTO PAN & ZOOM KE TITIK USER JIKA TIDAK ADA RUTE ---
        # Arahkan kamera tepat ke koordinat user
        iface.mapCanvas().setCenter(target_user_feat.geometry().asPoint())
        # Set skala zoom 1:2000 agar detail jalan terlihat
        iface.mapCanvas().zoomScale(2000) 
        iface.mapCanvas().refresh()
        # ---------------------------------------------------------------------
        
        QMessageBox.information(parent, "Selesai", f"Tidak ada rute <= 500 meter dengan port tersedia yang ditemukan.")
    
    print("--- SELESAI ---")

# Jalankan fungsi
run_routing_script_with_search()
