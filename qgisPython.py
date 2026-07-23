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
        if not f.hasGeometry() or f.geometry().isNull():
            continue
        
        raw_name = f['userPaniki'] if 'userPaniki' in f.fields().names() else f.id()
        nama_user = str(raw_name).strip() if raw_name else str(f.id())
        
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

    # 9. Request API OSRM
    for fat in fat_data:
        straight_dist = d_wgs.measureLine(user_pt_wgs, fat['point_wgs'])
        
        if straight_dist > 500:
            continue

        print(f"Menguji FAT: {fat['name']} [Sisa Port: {fat['idle']}] (Jarak Lurus: {round(straight_dist, 2)}m) ... ", end="")

        lon1, lat1 = user_pt_wgs.x(), user_pt_wgs.y()
        lon2, lat2 = fat['point_wgs'].x(), fat['point_wgs'].y()
        
        url_forward = f"http://router.project-osrm.org/route/v1/foot/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        url_backward = f"http://router.project-osrm.org/route/v1/foot/{lon2},{lat2};{lon1},{lat1}?overview=full&geometries=geojson"
        
        try:
            # A. Cek Rute Maju
            req = urllib.request.Request(url_forward, headers={'User-Agent': 'QGIS-PyQGIS-Script'})
            res = urllib.request.urlopen(req)
            data = json.loads(res.read().decode('utf-8'))
            
            route_dist = float('inf')
            route_coords = None
            
            if data['code'] == 'Ok':
                route_dist = data['routes'][0]['distance']
                route_coords = data['routes'][0]['geometry']['coordinates']
            
            # B. Jika rute mutar jauh (>500m), Trik Cek Arah Mundur (Melawan Arus)
            if route_dist > 500:
                time.sleep(0.1) # Jeda aman
                req_bw = urllib.request.Request(url_backward, headers={'User-Agent': 'QGIS-PyQGIS-Script'})
                res_bw = urllib.request.urlopen(req_bw)
                data_bw = json.loads(res_bw.read().decode('utf-8'))
                
                if data_bw['code'] == 'Ok':
                    dist_bw = data_bw['routes'][0]['distance']
                    # Jika jarak melawan arus ternyata pendek, gunakan jarak ini!
                    if dist_bw < route_dist: 
                        route_dist = dist_bw
                        route_coords = data_bw['routes'][0]['geometry']['coordinates']
                        print("[MELAWAN ARUS] ", end="")

            # C. Eksekusi Gambar Garis OSRM
            if route_dist <= 500 and route_coords:
                print(f"BERHASIL! Rute Jalan = {round(route_dist, 2)}m")
                points = [QgsPointXY(pt[0], pt[1]) for pt in route_coords]
                route_geom = QgsGeometry.fromPolylineXY(points)
                route_geom.transform(transform_to_local)
                
                new_feat = QgsFeature(line_layer.fields())
                new_feat.setGeometry(route_geom)
                new_feat.setAttributes([str(selected_user), str(fat['name']), int(fat['idle']), str(fat['olt']), str(fat['koordinat']), float(round(route_dist, 2))])
                new_features.append(new_feat)
                
            # D. Fallback: Jika di server mutar jauh, tapi aslinya berseberangan (<= 150m), tarik garis lurus
            elif straight_dist <= 150:
                print(f"KOREKSI! OSRM memutar jauh. Paksa tarik lurus: {round(straight_dist, 2)}m")
                route_geom = QgsGeometry.fromPolylineXY([user_pt_local, fat['point_local']])
                
                new_feat = QgsFeature(line_layer.fields())
                new_feat.setGeometry(route_geom)
                new_feat.setAttributes([str(selected_user), str(fat['name']), int(fat['idle']), str(fat['olt']), str(fat['koordinat']), float(round(straight_dist, 2))])
                new_features.append(new_feat)
            else:
                print(f"GAGAL (Rute jalan terlalu jauh: {round(route_dist, 2)}m)")
                
        except Exception as e:
            # Jika API internet putus, tapi jarak lurus sangat dekat, tetap buat garis lurus
            if straight_dist <= 150:
                 print(f"KOREKSI API ERROR! Paksa tarik lurus: {round(straight_dist, 2)}m")
                 route_geom = QgsGeometry.fromPolylineXY([user_pt_local, fat['point_local']])
                 new_feat = QgsFeature(line_layer.fields())
                 new_feat.setGeometry(route_geom)
                 new_feat.setAttributes([str(selected_user), str(fat['name']), int(fat['idle']), str(fat['olt']), str(fat['koordinat']), float(round(straight_dist, 2))])
                 new_features.append(new_feat)
            else:
                 print(f"ERROR JARINGAN API")
        
        time.sleep(0.2) 

    # 10. Tampilkan Hasil
    if new_features:
        line_layer.startEditing()
        line_layer.addFeatures(new_features)
        line_layer.commitChanges()
        line_layer.updateExtents()
        
        # Style Garis Merah 0.8 mm
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
        
        QMessageBox.information(parent, "Sukses", f"Ditemukan {len(new_features)} jalur rute valid yang port-nya tersedia.")
    else:
        iface.mapCanvas().setCenter(target_user_feat.geometry().asPoint())
        iface.mapCanvas().zoomScale(2000) 
        iface.mapCanvas().refresh()
        
        QMessageBox.information(parent, "Selesai", f"Tidak ada rute <= 500 meter dengan port tersedia yang ditemukan.")
    
    print("--- SELESAI ---")

# Jalankan fungsi
run_routing_script_with_search()
