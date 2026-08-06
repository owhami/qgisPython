import heapq
import os
from qgis.core import (
    QgsProject, QgsFeature, QgsGeometry, QgsVectorLayer,
    QgsDistanceArea, QgsField, QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsPointXY, QgsApplication,
    QgsSimpleLineSymbolLayer, QgsLineSymbol, QgsSingleSymbolRenderer,
    QgsMarkerSymbol, QgsSvgMarkerSymbolLayer,
    QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling
)
from qgis.utils import iface
from PyQt5.QtWidgets import QApplication, QInputDialog, QMessageBox
from PyQt5.QtCore import QVariant

def _cari_svg_camp():
    """Cari file SVG bawaan QGIS yang namanya mengandung kata 'camp'
    (mis. simbol tenda/perkemahan di library topo). Return path lengkap
    kalau ketemu, atau None kalau tidak ada di instalasi QGIS ini."""
    try:
        svg_paths = QgsApplication.svgPaths()
    except Exception:
        return None

    kandidat = []
    for base in svg_paths:
        if not base or not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith('.svg') and 'camp' in fn.lower():
                    kandidat.append(os.path.join(root, fn))

    if not kandidat:
        return None

    # Prioritaskan yang paling mendekati nama "camp" murni (bukan turunan lain)
    kandidat.sort(key=lambda p: len(os.path.basename(p)))
    return kandidat[0]

def run_routing_script_with_search():
    # ================== KONFIGURASI JARINGAN TIANG =================
    user_layer_name = 'tbUser'
    fat_layer_name = 'tbFAT'
    pole_layer_name = 'tbPole'

    RADIUS_TIANG_M = 500   # hanya tiang dalam radius ini dari user yang dimuat ke graf (performa)
    MAX_SPAN_M = 80        # jarak maksimal antar tiang yang dianggap 1 bentangan kabel
    MAX_DROP_M = 500       # jarak maksimal dari user/FAT ke tiang terdekat (kabel drop)
    K_TETANGGA = 5         # jumlah tetangga terdekat per tiang saat membangun graf

    parent = iface.mainWindow() if iface else None
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

    # ---------- 1. Layer wajib (dibutuhkan di kedua mode): FAT & Tiang ----------
    fat_layers = QgsProject.instance().mapLayersByName(fat_layer_name)
    pole_layers = QgsProject.instance().mapLayersByName(pole_layer_name)

    if not fat_layers:
        QMessageBox.critical(parent, "Error", f"Layer '{fat_layer_name}' tidak ditemukan!")
        return
    if not pole_layers:
        QMessageBox.critical(
            parent, "Error",
            f"Layer tiang '{pole_layer_name}' tidak ditemukan! "
            "Ganti nama di variabel pole_layer_name kalau nama layer aslimu berbeda."
        )
        return

    fat_layer = fat_layers[0]
    pole_layer = pole_layers[0]

    # ---------- 2. Popup validasi: apakah tbUser tersedia? ----------
    jawaban = QMessageBox.question(
        parent,
        "Validasi Layer tbUser",
        "Apakah layer 'tbUser' tersedia di project ini?\n\n"
        "• Pilih 'Yes' untuk mencari user dari tabel tbUser (isi nama userPaniki).\n"
        "• Pilih 'No' untuk memasukkan titik koordinat user secara manual.",
        QMessageBox.Yes | QMessageBox.No
    )
    mode_manual = (jawaban == QMessageBox.No)

    # CRS lokal acuan project. Default pakai CRS layer tiang (selalu ada di kedua mode).
    local_crs = pole_layer.crs()

    selected_user = None
    user_pt_local = None
    user_pt_wgs = None

    if not mode_manual:
        # ================= MODE A: CARI USER DARI tbUser (alur lama) =================
        user_layers = QgsProject.instance().mapLayersByName(user_layer_name)
        if not user_layers:
            QMessageBox.warning(
                parent, "Layer Tidak Ditemukan",
                f"Layer '{user_layer_name}' tidak ditemukan di project.\n"
                "Jalankan ulang script lalu pilih 'No' untuk input koordinat manual."
            )
            return

        user_layer = user_layers[0]
        local_crs = user_layer.crs()  # pakai CRS layer user sebagai acuan, sesuai perilaku asli

        # Daftar userPaniki
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

        # Pop-up Search User
        search_text, ok = QInputDialog.getText(
            parent, "Cari User PANIKI", "Ketik userPaniki (atau sebagian namanya):"
        )
        if not ok or not search_text.strip():
            print("Pencarian dibatalkan.")
            return

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

        target_user_feat = user_dict[selected_user]
        user_pt_local = target_user_feat.geometry().asPoint()

        transform_to_wgs84_user = QgsCoordinateTransform(local_crs, crs_wgs84, QgsProject.instance())
        user_geom_wgs = QgsGeometry(target_user_feat.geometry())
        user_geom_wgs.transform(transform_to_wgs84_user)
        user_pt_wgs = user_geom_wgs.asPoint()

    else:
        # ================= MODE B: INPUT KOORDINAT MANUAL =================
        coord_text, ok = QInputDialog.getText(
            parent,
            "Input Koordinat User",
            "Layer tbUser tidak digunakan.\n"
            "Masukkan koordinat titik user (format: latitude, longitude)\n"
            "Contoh: 1.487523, 124.845123"
        )
        if not ok or not coord_text.strip():
            print("Input koordinat dibatalkan.")
            return

        try:
            lat_str, lon_str = coord_text.strip().split(',')
            lat = float(lat_str.strip())
            lon = float(lon_str.strip())
        except (ValueError, AttributeError):
            QMessageBox.warning(
                parent, "Format Salah",
                "Format koordinat tidak valid. Gunakan format: latitude, longitude\nContoh: 1.487523, 124.845123"
            )
            return

        nama_input, ok2 = QInputDialog.getText(
            parent, "Nama/Label User", "Masukkan nama atau label untuk titik ini (boleh dikosongkan):"
        )
        selected_user = nama_input.strip() if (ok2 and nama_input.strip()) else f"Titik_{lat:.6f}_{lon:.6f}"

        user_pt_wgs = QgsPointXY(lon, lat)  # QGIS: x = longitude, y = latitude

        transform_from_wgs84 = QgsCoordinateTransform(crs_wgs84, local_crs, QgsProject.instance())
        user_geom_local = QgsGeometry.fromPointXY(user_pt_wgs)
        user_geom_local.transform(transform_from_wgs84)
        user_pt_local = user_geom_local.asPoint()

    print(f"\n--- MEMULAI PENCARIAN UNTUK: {selected_user} ---")

    # ---------- 3. Alat ukur jarak & transform (dipakai bersama kedua mode) ----------
    d_wgs = QgsDistanceArea()
    d_wgs.setSourceCrs(crs_wgs84, QgsProject.instance().transformContext())
    d_wgs.setEllipsoid('WGS84')

    transform_to_wgs84 = QgsCoordinateTransform(local_crs, crs_wgs84, QgsProject.instance())

    # Pan/zoom kanvas ke titik user SEBELUM proses pencarian & routing FAT dimulai.
    iface.mapCanvas().setCenter(user_pt_local)
    iface.mapCanvas().zoomScale(2000)
    iface.mapCanvas().refresh()
    QApplication.processEvents()

    # ---------- 3b. Tandai titik user dengan simbol "topo camp" ----------
    # Hanya dibuat kalau user TIDAK punya tbUser (mode input koordinat manual).
    if mode_manual:
        user_marker_layer = QgsVectorLayer(f"Point?crs={local_crs.authid()}", f"Titik_Manual_{selected_user}", "memory")
        marker_provider = user_marker_layer.dataProvider()
        marker_provider.addAttributes([QgsField("nama_user", QVariant.String)])
        user_marker_layer.updateFields()

        marker_feat = QgsFeature(user_marker_layer.fields())
        marker_feat.setGeometry(QgsGeometry.fromPointXY(user_pt_local))
        marker_feat.setAttributes([str(selected_user)])

        user_marker_layer.startEditing()
        user_marker_layer.addFeature(marker_feat)
        user_marker_layer.commitChanges()
        user_marker_layer.updateExtents()

        camp_svg_path = _cari_svg_camp()
        if camp_svg_path:
            svg_symbol_layer = QgsSvgMarkerSymbolLayer(camp_svg_path)
            svg_symbol_layer.setSize(12)
            marker_symbol = QgsMarkerSymbol()
            marker_symbol.changeSymbolLayer(0, svg_symbol_layer)
            print(f"Simbol 'camp' ditemukan: {camp_svg_path}")
        else:
            # Fallback kalau tidak ada SVG 'camp' di instalasi QGIS ini
            marker_symbol = QgsMarkerSymbol.createSimple({
                'name': 'star',
                'color': '255,140,0,255',
                'size': '12'
            })
            print("Peringatan: SVG simbol 'camp' tidak ditemukan di library QGIS, memakai simbol bintang oranye sebagai gantinya.")

        # Beri label kecil di titik supaya nama/labelnya terbaca di kanvas
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = "nama_user"
        text_format = QgsTextFormat()
        text_format.setSize(9)
        label_settings.setFormat(text_format)
        user_marker_layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        user_marker_layer.setLabelsEnabled(True)

        user_marker_layer.setRenderer(QgsSingleSymbolRenderer(marker_symbol))
        QgsProject.instance().addMapLayer(user_marker_layer)

    # ---------- 4. Cache data FAT ----------
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

    # ---------- 5. Cache data Tiang (hanya dalam RADIUS_TIANG_M dari user) ----------
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

    # ---------- 6. Bangun graf: tiap tiang terhubung ke K tetangga terdekatnya ----------
    print(f"Membangun graf jaringan tiang (span maks {MAX_SPAN_M}m, {K_TETANGGA} tetangga terdekat/tiang)...")

    graph = {p['id']: [] for p in pole_points}
    n = len(pole_points)
    for i in range(n):
        dists = []
        for j in range(n):
            if i == j:
                continue
            dist = d_wgs.measureLine(pole_points[i]['point_wgs'], pole_points[j]['point_wgs'])
            if dist <= MAX_SPAN_M:
                dists.append((dist, pole_points[j]['id']))
        dists.sort(key=lambda x: x[0])

        pid = pole_points[i]['id']
        for dist, neighbor_id in dists[:K_TETANGGA]:
            if not any(nb == neighbor_id for nb, _ in graph[pid]):
                graph[pid].append((neighbor_id, dist))
            if not any(nb == pid for nb, _ in graph[neighbor_id]):
                graph[neighbor_id].append((pid, dist))

    print(f"-> Graf ({sum(len(v) for v in graph.values()) // 2} bentangan) selesai dibangun.")

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

    # ---------- 7. Layer Output ----------
    layer_name = f"Rute_{selected_user}_Tiang"
    line_layer = QgsVectorLayer(f"LineString?crs={local_crs.authid()}", layer_name, "memory")
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

    # ---------- 8. Routing lewat jaringan tiang untuk tiap FAT ----------
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
              f"(drop_user={round(drop_user, 2)}m + jaringan={round(path_dist, 2)}m + drop_fat={round(drop_fat, 2)}m) "
              f"-- lewat: {' -> '.join(pole_by_id[pid]['label'] for pid in path_ids)}")

        new_feat = QgsFeature(line_layer.fields())
        new_feat.setGeometry(route_geom)
        new_feat.setAttributes([
            str(selected_user), str(fat['name']), int(fat['idle']), str(fat['olt']),
            str(fat['koordinat']), float(round(total_dist, 2)), int(len(path_ids))
        ])
        new_features.append(new_feat)

    # ---------- 9. Tampilkan Hasil ----------
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
        iface.mapCanvas().setCenter(user_pt_local)
        iface.mapCanvas().zoomScale(2000)
        iface.mapCanvas().refresh()

        QMessageBox.information(parent, "Selesai", "Tidak ada rute FAT via jaringan tiang dalam radius 500m.")

    print("=== END ===")

run_routing_script_with_search()
