import heapq
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
    # ================== KONFIGURASI JARINGAN TIANG ==================
    # GANTI nama layer tiang ini sesuai nama layer asli di proyekmu
    pole_layer_name = 'tbPole'
    RADIUS_TIANG_M = 500  
    MAX_SPAN_M = 80       
    MAX_DROP_M = 500      

    # 1. Nama layer
    user_layer_name = 'tbUser'
    fat_layer_name = 'tbFAT'

    user_layers = QgsProject.instance().mapLayersByName(user_layer_name)
    fat_layers = QgsProject.instance().mapLayersByName(fat_layer_name)
    pole_layers = QgsProject.instance().mapLayersByName(pole_layer_name)

    if not user_layers or not fat_layers:
        print("Error: Layer tbUser atau tbFAT tidak ditemukan!")
        return

    if not pole_layers:
        print(f"Error: Layer tiang '{pole_layer_name}' tidak ditemukan! Ganti nama di variabel pole_layer_name kalau nama layer aslimu berbeda.")
        return

    user_layer = user_layers[0]
    fat_layer = fat_layers[0]
    pole_layer = pole_layers[0]

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

    # Langsung pan/zoom kanvas ke titik user yang dipilih, SEBELUM proses
    # pencarian & routing FAT dimulai.
    iface.mapCanvas().setCenter(user_pt_local)
    iface.mapCanvas().zoomScale(2000)
    iface.mapCanvas().refresh()
    QApplication.processEvents()

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

    if not fat_data:
        pesan = f"Tidak ada FAT dengan port tersedia (usedSPLT < 8) di layer '{fat_layer_name}'. Tidak bisa membuat rute untuk '{selected_user}'."
        print(pesan)
        QMessageBox.warning(parent, "Tidak Ada FAT", pesan)
        return

    # 7b. Cache data Tiang (hanya yang dalam RADIUS_TIANG_M dari user, biar graf tidak kebesaran)
    print(f"Memuat titik tiang dalam radius {RADIUS_TIANG_M}m dari user...")
    pole_points = []
    for i, f in enumerate(pole_layer.getFeatures()):
        if not f.hasGeometry() or f.geometry().isNull():
            continue

        p_local = f.geometry().asPoint()
        p_geom_wgs = QgsGeometry(f.geometry())
        p_geom_wgs.transform(transform_to_wgs84)
        p_wgs = p_geom_wgs.asPoint()

        dist_from_user = d_wgs.measureLine(user_pt_wgs, p_wgs)
        if dist_from_user > RADIUS_TIANG_M:
            continue

        id_tiang = f['idTiang'] if 'idTiang' in f.fields().names() else f.id()

        pole_points.append({
            'id': i,
            'label': str(id_tiang),
            'point_wgs': p_wgs,
            'point_local': p_local
        })

    print(f"-> {len(pole_points)} tiang dimuat.")

    if not pole_points:
        pesan = f"Tidak ada tiang dalam radius {RADIUS_TIANG_M}m dari user '{selected_user}'. Tidak bisa membuat rute."
        print(pesan)
        QMessageBox.warning(parent, "Tidak Ada Tiang", pesan)
        return

    # 7c. Bangun graf: dua tiang dianggap tersambung (1 bentangan kabel) kalau
    # jaraknya <= MAX_SPAN_M
    print(f"Membangun graf jaringan tiang (span maks {MAX_SPAN_M}m)...")
    graph = {p['id']: [] for p in pole_points}
    n = len(pole_points)
    for i in range(n):
        for j in range(i + 1, n):
            dist = d_wgs.measureLine(pole_points[i]['point_wgs'], pole_points[j]['point_wgs'])
            if dist <= MAX_SPAN_M:
                graph[pole_points[i]['id']].append((pole_points[j]['id'], dist))
                graph[pole_points[j]['id']].append((pole_points[i]['id'], dist))
    print("-> Graf selesai dibangun.")

    pole_by_id = {p['id']: p for p in pole_points}

    def dijkstra(start_id, end_id):
        """Cari jalur terpendek antar dua tiang di graf.
        Return (jarak_total, [list_id_tiang_berurutan]) atau (None, None) kalau tidak tersambung."""
        if start_id == end_id:
            return 0.0, [start_id]
        dist = {start_id: 0.0}
        prev = {}
        visited = set()
        pq = [(0.0, start_id)]
        while pq:
            d, node = heapq.heappop(pq)
            if node in visited:
                continue
            visited.add(node)
            if node == end_id:
                path = [node]
                while path[-1] != start_id:
                    path.append(prev[path[-1]])
                path.reverse()
                return d, path
            for neighbor, weight in graph.get(node, []):
                nd = d + weight
                if neighbor not in dist or nd < dist[neighbor]:
                    dist[neighbor] = nd
                    prev[neighbor] = node
                    heapq.heappush(pq, (nd, neighbor))
        return None, None

    def nearest_pole(point_wgs, max_dist):
        """Cari tiang terdekat dari suatu titik, dalam batas max_dist (kabel drop).
        Return (dict_tiang, jarak) atau (None, None) kalau tidak ada tiang dalam jangkauan."""
        best = None
        best_dist = None
        for p in pole_points:
            dist = d_wgs.measureLine(point_wgs, p['point_wgs'])
            if dist <= max_dist and (best_dist is None or dist < best_dist):
                best = p
                best_dist = dist
        return best, best_dist

    # 8. Layer Output
    layer_name = f"Rute_{selected_user}_Tiang"
    line_layer = QgsVectorLayer(f"LineString?crs={user_layer.crs().authid()}", layer_name, "memory")
    provider = line_layer.dataProvider()
    
    provider.addAttributes([
        QgsField("userPaniki", QVariant.String),
        QgsField("idFAT", QVariant.String),
        QgsField("usedSPLT", QVariant.Int),
        QgsField("idOLT", QVariant.String),
        QgsField("koordinatFAT", QVariant.String),
        QgsField("jarak_jalan_m", QVariant.Double),
        QgsField("jml_tiang", QVariant.Int)
    ])
    line_layer.updateFields()

    new_features = []

    # 9. Routing lewat jaringan tiang (bukan lewat OSRM lagi) -- kabel drop
    # (user->tiang & tiang->FAT) dihitung garis lurus (sesuai praktik FTTH
    # nyata), jalur antar tiang dicari lewat Dijkstra di graf yang sudah dibangun.
    for fat in fat_data:
        straight_dist = d_wgs.measureLine(user_pt_wgs, fat['point_wgs'])
        
        if straight_dist > 500:
            continue

        print(f"Menguji FAT: {fat['name']} [Sisa Port: {fat['idle']}] (Jarak Lurus: {round(straight_dist, 2)}m) ... ", end="")

        pole_user, drop_user = nearest_pole(user_pt_wgs, MAX_DROP_M)
        pole_fat, drop_fat = nearest_pole(fat['point_wgs'], MAX_DROP_M)

        if pole_user is None or pole_fat is None:
            print(f"GAGAL (tidak ada tiang dalam jangkauan drop {MAX_DROP_M}m dari user/FAT)")
            continue

        path_dist, path_ids = dijkstra(pole_user['id'], pole_fat['id'])

        if path_dist is None:
            print("GAGAL (tiang user & FAT tidak tersambung di jaringan tiang -- cek span/data tiang)")
            continue

        total_dist = drop_user + path_dist + drop_fat
        if total_dist > 500:
            print(f"GAGAL (total rute via tiang {round(total_dist, 2)}m > 500m)")
            continue

        route_points_local = [user_pt_local]
        for pid in path_ids:
            route_points_local.append(pole_by_id[pid]['point_local'])
        route_points_local.append(fat['point_local'])

        route_geom = QgsGeometry.fromPolylineXY(route_points_local)

        print(f"BERHASIL! [via {len(path_ids)} tiang] Rute = {round(total_dist, 2)}m "
              f"(drop_user={round(drop_user, 2)}m + jaringan={round(path_dist, 2)}m + drop_fat={round(drop_fat, 2)}m)")

        new_feat = QgsFeature(line_layer.fields())
        new_feat.setGeometry(route_geom)
        new_feat.setAttributes([
            str(selected_user), str(fat['name']), int(fat['idle']), str(fat['olt']),
            str(fat['koordinat']), float(round(total_dist, 2)), int(len(path_ids))
        ])
        new_features.append(new_feat)

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
        
        QMessageBox.information(parent, "Sukses", f"Ditemukan {len(new_features)} jalur rute (mengikuti jaringan tiang) yang port-nya tersedia.")
    else:
        iface.mapCanvas().setCenter(target_user_feat.geometry().asPoint())
        iface.mapCanvas().zoomScale(2000) 
        iface.mapCanvas().refresh()
        
        QMessageBox.information(parent, "Selesai", "Tidak ada rute FAT via jaringan tiang yang valid ditemukan dalam radius 500m.")
    
    print("=== SELESAI ===")

run_routing_script_with_search()
