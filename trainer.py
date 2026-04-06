"""
trainer.py — Отдельный сервис обучения YOLOv8.
Запуск: python trainer.py  (порт 5001)

Взаимодействует с основным приложением (app.py, порт 5000) через HTTP:
  - принимает POST /train           — запуск задачи обучения
  - отдаёт  GET  /job_status/<id>   — текущий статус задачи
  - отдаёт  GET  /jobs              — список всех задач
  - шлёт    POST callback в app.py  — при завершении/ошибке
"""

from flask import Flask, request, jsonify
import threading
import uuid
import os
import json
import time
import requests
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="[TRAINER] %(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Настройки путей (должны совпадать с app.py) ──────────────────────────────
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
MODELS_FOLDER = os.path.join(os.path.dirname(__file__), "static", "models")
DATASETS_FOLDER = os.path.join(os.path.dirname(__file__), "static", "datasets")

# URL обратного вызова в основное приложение
MAIN_APP_CALLBACK_URL = os.environ.get("MAIN_APP_URL", "http://127.0.0.1:5000")

# ── Хранилище задач (in-memory) ───────────────────────────────────────────────
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def update_job(job_id: str, **kwargs):
    """Обновить поля задачи потокобезопасно."""
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            jobs[job_id]["updated_at"] = datetime.now().isoformat()


def send_callback(job_id: str):
    """Отправить результат задачи в основное приложение."""
    with jobs_lock:
        job = jobs.get(job_id, {}).copy()

    try:
        resp = requests.post(
            f"{MAIN_APP_CALLBACK_URL}/trainer_callback",
            json=job,
            timeout=10,
        )
        log.info(f"Callback sent for job {job_id}: HTTP {resp.status_code}")
    except Exception as e:
        log.warning(f"Callback failed for job {job_id}: {e}")


def build_yolo_dataset(images_info: list[dict], job_id: str) -> str:
    """
    Подготовить датасет в формате YOLO из переданных изображений.

    images_info — список словарей:
        {
            "filename": "abc123.jpg",          # имя файла в static/uploads
            "annotations": [
                {"x": 10, "y": 20, "width": 50, "height": 60, "label": "cat"},
                ...
            ]
        }

    Возвращает путь к dataset.yaml.
    """
    from PIL import Image as PILImage

    dataset_dir = os.path.join(DATASETS_FOLDER, job_id)
    train_images = os.path.join(dataset_dir, "images", "train")
    train_labels = os.path.join(dataset_dir, "labels", "train")
    os.makedirs(train_images, exist_ok=True)
    os.makedirs(train_labels, exist_ok=True)

    # Собираем уникальные классы
    all_labels = sorted(set(
        a["label"]
        for img in images_info
        for a in img.get("annotations", [])
        if a.get("label")
    ))
    label_to_id = {l: i for i, l in enumerate(all_labels)}

    skipped = 0
    for img_info in images_info:
        src = os.path.join(UPLOAD_FOLDER, img_info["filename"])
        if not os.path.exists(src):
            log.warning(f"Image not found: {src}")
            skipped += 1
            continue

        # Копируем изображение
        import shutil
        dst = os.path.join(train_images, img_info["filename"])
        shutil.copy2(src, dst)

        annotations = img_info.get("annotations", [])
        if not annotations:
            continue

        # Реальные размеры изображения для нормализации
        try:
            with PILImage.open(src) as pil_img:
                img_w, img_h = pil_img.size
        except Exception:
            img_w, img_h = 640, 640

        # Пишем YOLO-аннотации
        base = os.path.splitext(img_info["filename"])[0]
        label_path = os.path.join(train_labels, f"{base}.txt")

        # КРИТИЧНО: определяем, в каких единицах хранятся координаты в БД.
        # Если аннотатор сохранял пиксельные координаты — делим на размер.
        # Если уже нормализованные (0..1) — делить не нужно.
        # Эвристика: если x > 1.5 — координаты пиксельные.
        sample_x = annotations[0].get("x", 0) if annotations else 0
        coords_are_pixels = (sample_x > 1.5)

        written = 0
        with open(label_path, "w") as f:
            for a in annotations:
                x, y, w_ann, h_ann = a.get("x", 0), a.get("y", 0), a.get("width", 0), a.get("height", 0)

                if coords_are_pixels:
                    # Координаты пиксельные — нормализуем
                    cx = (x + w_ann / 2) / img_w
                    cy = (y + h_ann / 2) / img_h
                    w_n = w_ann / img_w
                    h_n = h_ann / img_h
                else:
                    # Уже нормализованы (cx, cy, w, h формат)
                    cx, cy, w_n, h_n = x, y, w_ann, h_ann

                # Клипаем на случай выхода за границы
                cx   = max(0.0, min(1.0, cx))
                cy   = max(0.0, min(1.0, cy))
                w_n  = max(0.001, min(1.0, w_n))
                h_n  = max(0.001, min(1.0, h_n))

                # Пропускаем вырожденные боксы
                if w_n < 0.001 or h_n < 0.001:
                    log.warning(f"Degenerate bbox skipped: {a}")
                    continue

                cls = label_to_id.get(a.get("label", ""), 0)
                f.write(f"{cls} {cx:.6f} {cy:.6f} {w_n:.6f} {h_n:.6f}\n")
                written += 1

        log.info(f"  {img_info['filename']}: {written} annotations, img_size={img_w}x{img_h}, pixels={coords_are_pixels}")

    log.info(f"Dataset built: {len(images_info) - skipped} images, {len(all_labels)} classes, skipped {skipped}")

    # Пишем dataset.yaml — names должны быть в YAML-формате, не Python repr
    # НЕПРАВИЛЬНО: names: ['cat', 'dog']   ← ultralytics не распознаёт
    # ПРАВИЛЬНО:   names:
    #                - cat
    #                - dog
    yaml_path = os.path.join(dataset_dir, "dataset.yaml")
    names_yaml_block = "\n".join(f"  - {lbl}" for lbl in all_labels)
    with open(yaml_path, "w") as f:
        f.write(f"path: {os.path.abspath(dataset_dir)}\n")  # абсолютный путь — важно!
        f.write("train: images/train\n")
        f.write("val: images/train\n")   # при малом датасете val = train
        f.write(f"nc: {len(all_labels)}\n")
        f.write(f"names:\n{names_yaml_block}\n")

    # Верификация — выводим yaml в лог чтобы можно было проверить
    with open(yaml_path) as yf:
        log.info(f"dataset.yaml:\n{yf.read()}")

    return yaml_path


def run_training(job_id: str, payload: dict):
    """
    Основной поток обучения.
    Запускается в отдельном Thread, не блокирует Flask.
    """
    log.info(f"Training started for job {job_id}")
    update_job(job_id, status="preparing", progress=5)

    try:
        images_info = payload.get("images", [])
        epochs      = int(payload.get("epochs", 10))
        imgsz       = int(payload.get("imgsz", 640))
        model_base  = payload.get("model", "yolov8n.pt")   # yolov8n / yolov8s / ...

        if not images_info:
            raise ValueError("Нет изображений для обучения")

        # ── 1. Подготовка датасета ──────────────────────────────────────
        update_job(job_id, status="preparing", progress=10, message="Подготовка датасета...")
        yaml_path = build_yolo_dataset(images_info, job_id)

        # ── 2. Импорт ultralytics (YOLO) ────────────────────────────────
        update_job(job_id, status="training", progress=20, message="Загрузка модели...")
        try:
            from ultralytics import YOLO
        except ImportError:
            raise RuntimeError(
                "Библиотека ultralytics не установлена. "
                "Выполните: pip install ultralytics"
            )

        model = YOLO(model_base)

        # ── 3. Обучение с колбэком прогресса ────────────────────────────
        update_job(job_id, status="training", progress=25, message="Обучение...")
        os.makedirs(MODELS_FOLDER, exist_ok=True)

        project_dir = os.path.join(DATASETS_FOLDER, job_id, "runs")
        # Минимум 50 эпох для нормального обучения на малом датасете.
        # patience=20 — ранняя остановка если нет улучшений.
        # conf=0.001 — низкий порог при обучении (высокий только при инференсе).
        # augment=True — аугментация критически важна при малом датасете.
        effective_epochs = max(epochs, 50)
        results = model.train(
            data=yaml_path,
            epochs=effective_epochs,
            imgsz=imgsz,
            project=project_dir,
            name="train",
            exist_ok=True,
            verbose=True,       # True чтобы видеть прогресс в логах
            patience=20,        # ранняя остановка
            batch=-1,           # авто-подбор batch size под GPU/CPU
            cache=False,        # не кэшировать если мало RAM
            augment=True,       # аугментация — важна при малом датасете
            cos_lr=True,        # косинусный lr schedule
            warmup_epochs=3,
        )

        # ── 4. Копирование лучшей модели в shared folder ────────────────
        update_job(job_id, status="exporting", progress=90, message="Экспорт модели...")

        best_pt = os.path.join(project_dir, "train", "weights", "best.pt")
        if not os.path.exists(best_pt):
            # Fallback: last.pt
            best_pt = os.path.join(project_dir, "train", "weights", "last.pt")

        if not os.path.exists(best_pt):
            raise FileNotFoundError("Файл весов не найден после обучения")

        # Конвертируем в ONNX для совместимости с inference в app.py
        onnx_name = f"trained_{job_id[:8]}.onnx"
        onnx_path = os.path.join(MODELS_FOLDER, onnx_name)

        trained_model = YOLO(best_pt)
        # opset=12 — стабильная версия для onnxruntime
        # simplify=True — убирает лишние операции из графа
        # dynamic=False — фиксированный batch, нужен для корректного инференса
        trained_model.export(
            format="onnx",
            imgsz=imgsz,
            opset=12,
            simplify=True,
            dynamic=False,
        )

        # ultralytics сохраняет рядом с best.pt
        exported_onnx = best_pt.replace(".pt", ".onnx")
        if os.path.exists(exported_onnx):
            import shutil
            shutil.move(exported_onnx, onnx_path)
        else:
            # Если ONNX не создался — сохраняем .pt
            onnx_name = f"trained_{job_id[:8]}.pt"
            onnx_path = os.path.join(MODELS_FOLDER, onnx_name)
            import shutil
            shutil.copy2(best_pt, onnx_path)

        # ── 5. Метрики ──────────────────────────────────────────────────
        metrics = {}
        try:
            metrics_file = os.path.join(project_dir, "train", "results.csv")
            if os.path.exists(metrics_file):
                import csv
                with open(metrics_file) as mf:
                    reader = csv.DictReader(mf)
                    rows = list(reader)
                    if rows:
                        last = rows[-1]
                        metrics = {k.strip(): v.strip() for k, v in last.items()}
        except Exception as me:
            log.warning(f"Could not read metrics: {me}")

        update_job(
            job_id,
            status="done",
            progress=100,
            message="Обучение завершено",
            model_filename=onnx_name,
            metrics=metrics,
        )
        log.info(f"Job {job_id} completed. Model: {onnx_name}")

    except Exception as e:
        log.exception(f"Training failed for job {job_id}: {e}")
        update_job(job_id, status="error", progress=0, message=str(e))

    finally:
        send_callback(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP эндпоинты
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/train", methods=["POST"])
def start_training():
    """
    Запустить обучение.

    Тело запроса (JSON):
    {
        "project_id": 1,
        "images": [
            {
                "filename": "abc.jpg",
                "annotations": [{"x":10,"y":20,"width":50,"height":60,"label":"cat"}]
            }
        ],
        "epochs": 10,
        "imgsz": 640,
        "model": "yolov8n.pt"
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Пустой запрос"}), 400

    job_id = uuid.uuid4().hex

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "project_id": data.get("project_id"),
            "status": "queued",
            "progress": 0,
            "message": "В очереди",
            "model_filename": None,
            "metrics": {},
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

    # Запускаем в отдельном потоке, чтобы не блокировать HTTP
    t = threading.Thread(target=run_training, args=(job_id, data), daemon=True)
    t.start()

    log.info(f"Job {job_id} queued")
    return jsonify({"success": True, "job_id": job_id})


@app.route("/job_status/<job_id>", methods=["GET"])
def job_status(job_id):
    """Текущий статус задачи обучения."""
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"success": False, "error": "Задача не найдена"}), 404

    return jsonify({"success": True, **job})


@app.route("/jobs", methods=["GET"])
def list_jobs():
    """Список всех задач (для отладки / отображения истории)."""
    with jobs_lock:
        all_jobs = list(jobs.values())

    return jsonify({"success": True, "jobs": all_jobs})


@app.route("/health", methods=["GET"])
def health():
    """Проверка живости сервиса."""
    import psutil
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    try:
        import torch
        gpu_ok = torch.cuda.is_available()
    except ImportError:
        gpu_ok = False

    active = sum(1 for j in jobs.values() if j["status"] in ("preparing", "training", "exporting"))

    return jsonify({
        "success": True,
        "online": True,
        "cpu_percent": cpu,
        "ram_percent": ram,
        "gpu_available": gpu_ok,
        "active_jobs": active,
        "total_jobs": len(jobs),
    })



@app.route("/debug_dataset/<job_id>", methods=["GET"])
def debug_dataset(job_id):
    """
    Отладочный эндпоинт: показывает содержимое датасета для задачи.
    Используйте ДО обучения чтобы проверить что координаты корректны.
    GET /debug_dataset/<job_id>
    """
    dataset_dir = os.path.join(DATASETS_FOLDER, job_id)
    labels_dir  = os.path.join(dataset_dir, "labels", "train")
    yaml_path   = os.path.join(dataset_dir, "dataset.yaml")

    if not os.path.exists(dataset_dir):
        return jsonify({"error": "Dataset not found"}), 404

    result = {"job_id": job_id, "files": [], "yaml": None}

    if os.path.exists(yaml_path):
        with open(yaml_path) as f:
            result["yaml"] = f.read()

    if os.path.exists(labels_dir):
        for fname in os.listdir(labels_dir)[:5]:   # первые 5
            fpath = os.path.join(labels_dir, fname)
            with open(fpath) as f:
                lines = f.readlines()
            parsed = []
            for line in lines:
                parts = line.strip().split()
                if len(parts) == 5:
                    cls, cx, cy, w, h = parts
                    parsed.append({
                        "cls": int(cls), "cx": float(cx), "cy": float(cy),
                        "w": float(w), "h": float(h),
                        "valid": 0 < float(cx) < 1 and 0 < float(cy) < 1
                                 and 0 < float(w) < 1 and 0 < float(h) < 1,
                    })
            result["files"].append({"name": fname, "annotations": parsed})

    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(MODELS_FOLDER, exist_ok=True)
    os.makedirs(DATASETS_FOLDER, exist_ok=True)

    log.info("Trainer service starting on port 5001...")
    app.run(host="0.0.0.0", port=5001, debug=False)