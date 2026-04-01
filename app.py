from flask import Flask, render_template, redirect, url_for, flash, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MODELS_FOLDER'] = 'static/models'

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ======== Роли ========
class Role:
    ADMIN = 'admin'
    ANNOTATOR = 'annotator'

# ======== Модели ========
user_project = db.Table('user_project',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('project_id', db.Integer, db.ForeignKey('project.id'), primary_key=True),
    db.Column('role', db.String(20), default=Role.ANNOTATOR)
)


class Model(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)  # оригинальное имя файла
    filename = db.Column(db.String(200), nullable=False)  # uuid_name.onnx
    format = db.Column(db.String(20), default='onnx')
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=db.func.now())

    def __repr__(self):
        return f"<Model {self.name}>"

#
# @app.route('/test')
# @login_required
# def project_models_list(project_id):
#     project = Project.query.get_or_404(project_id)
#     if not get_user_role(current_user, project):
#         return jsonify({"success": False, "error": "Нет доступа"})
#     models = Model.query.filter_by(project_id=project_id).order_by(Model.created_at.desc()).all()
#     return jsonify({
#         "success": True,
#         "models": [{"id": m.id, "name": m.name, "filename": m.filename} for m in models]
#     })


@app.route('/project/<int:project_id>/models_list')
@login_required
def project_models_list(project_id):
    project = Project.query.get_or_404(project_id)
    if not get_user_role(current_user, project):
        return jsonify({"success": False, "error": "Нет доступа"})
    models = Model.query.filter_by(project_id=project_id).order_by(Model.created_at.desc()).all()
    return jsonify({
        "success": True,
        "models": [{"id": m.id, "name": m.name, "filename": m.filename} for m in models]
    })


@app.route('/project/<int:project_id>/inference')
@login_required
def project_inference(project_id):
    project = Project.query.get_or_404(project_id)

    role = get_user_role(current_user, project)
    if not role:
        flash("Нет доступа к проекту", "danger")
        return redirect(url_for('project_select'))

    if role != Role.ADMIN:
        flash("Инференс доступен только администратору", "danger")
        return redirect(url_for('project_dashboard', project_id=project_id))

    raw_images = Image.query.filter_by(project_id=project_id).all()

    images = []
    for img in raw_images:
        images.append({
            "id": img.id,
            "filename": img.filename,
            "annotations": [
                {
                    "x": a.x, "y": a.y,
                    "width": a.width, "height": a.height,
                    "label": a.label
                }
                for a in img.annotations
            ]
        })

    # ← теперь вне цикла
    models = Model.query.filter_by(project_id=project_id).order_by(Model.created_at.desc()).all()

    import json
    images_json = json.dumps(images)

    return render_template(
        'inference.html',
        project=project,
        images=images,
        images_json=images_json,
        models=models,
        role=role
    )

@app.route('/project/<int:project_id>/upload_model', methods=['POST'])
@login_required
def upload_model(project_id):
    project = Project.query.get_or_404(project_id)

    if get_user_role(current_user, project) != Role.ADMIN:
        return jsonify({"success": False, "error": "Нет доступа"})

    file = request.files.get('model')
    if not file or file.filename == '':
        return jsonify({"success": False, "error": "Файл не выбран"})

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {'.onnx'}:
        return jsonify({"success": False, "error": "Поддерживается только .onnx"})

    from werkzeug.utils import secure_filename
    import uuid

    original_name = secure_filename(file.filename)
    filename = f"{uuid.uuid4().hex}_{original_name}"

    os.makedirs(app.config['MODELS_FOLDER'], exist_ok=True)
    filepath = os.path.join(app.config['MODELS_FOLDER'], filename)
    file.save(filepath)

    # Сохраняем в БД
    model = Model(
        name=original_name,
        filename=filename,
        format='onnx',
        project_id=project_id,
        uploaded_by=current_user.id
    )
    db.session.add(model)
    db.session.commit()

    return jsonify({
        "success": True,
        "model": {
            "id": model.id,
            "name": model.name,
            "filename": model.filename
        }
    })


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    team = db.Column(db.String(100), default="Моя команда")
    projects = db.relationship('Project', secondary=user_project, back_populates='users')
    annotations = db.relationship('Annotation', backref='creator', lazy=True)


@app.route('/project/<int:project_id>/delete_model/<int:model_id>', methods=['POST'])
@login_required
def delete_model(project_id, model_id):
    project = Project.query.get_or_404(project_id)

    if get_user_role(current_user, project) != Role.ADMIN:
        return jsonify({"success": False, "error": "Нет доступа"})

    model = Model.query.get_or_404(model_id)

    filepath = os.path.join(app.config['MODELS_FOLDER'], model.filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    db.session.delete(model)
    db.session.commit()

    return jsonify({"success": True})


class Project(db.Model):



    id = db.Column(db.Integer, primary_key=True)

    # название проекта
    name = db.Column(db.String(120), nullable=False)

    # описание
    description = db.Column(db.Text)

    # статус проекта
    status = db.Column(
        db.String(20),
        default="active"   # active / completed / paused
    )

    # дата создания
    created_at = db.Column(
        db.DateTime,
        default=db.func.now()
    )

    # дата последнего обновления
    updated_at = db.Column(
        db.DateTime,
        default=db.func.now(),
        onupdate=db.func.now()
    )

    # пользователи проекта
    users = db.relationship(
        "User",
        secondary=user_project,
        back_populates="projects"
    )

    # изображения проекта
    images = db.relationship(
        "Image",
        backref="project",
        lazy=True,
        cascade="all, delete"
    )

    # --- удобные свойства ---

    @property
    def image_count(self):
        return len(self.images)

    def __repr__(self):
        return f"<Project {self.name}>"

class Image(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    annotations = db.relationship('Annotation', backref='image', lazy=True)

class Annotation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey('image.id'), nullable=False)
    x = db.Column(db.Float)
    y = db.Column(db.Float)
    width = db.Column(db.Float)
    height = db.Column(db.Float)
    label = db.Column(db.String(50))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))


def get_user_role(user, project):
    link = db.session.execute(
        user_project.select().where(
            (user_project.c.user_id == user.id) &
            (user_project.c.project_id == project.id)
        )
    ).first()
    return link.role if link else None

# ======== Декоратор для роли ========
def role_required(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if current_user.role != role:
                flash("Доступ запрещён", "danger")
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ======== Flask-Login ========
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ======== Маршруты ========
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # Берём значения прямо по name из формы
        full_name = request.form.get('fullName')  # имя и фамилия
        email = request.form.get('email')
        password_raw = request.form.get('password')
        team = request.form.get('team', 'Моя команда')
        terms_accepted = request.form.get('terms')  # если чекбокс не отмечен, будет None

        # Проверка обязательных полей
        if not full_name or not email or not password_raw:
            flash("Заполните все обязательные поля", "danger")
            return redirect(url_for('register'))

        if not terms_accepted:
            flash("Необходимо принять условия использования", "danger")
            return redirect(url_for('register'))

        # Хэшируем пароль
        password_hashed = generate_password_hash(password_raw, method='pbkdf2:sha256')
        # Проверяем, что email ещё не зарегистрирован
        if User.query.filter_by(email=email).first():
            flash("Email уже зарегистрирован", "danger")
            return redirect(url_for('register'))

        # Создаём пользователя
        user = User(username=full_name, email=email, password=password_hashed, team=team)
        db.session.add(user)
        db.session.commit()

        flash("Аккаунт создан", "success")
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/project_select')
@login_required
def project_select():
    projects = current_user.projects  # все проекты, в которых участвует пользователь

    projects_info = []
    for project in projects:
        role = get_user_role(current_user, project)
        projects_info.append({
            "project": project,
            "role": role
        })

    return render_template(
        "project_select.html",
        projects_info=projects_info
    )


@app.route('/create_project', methods=['POST'])
@login_required
def create_project():
    # получаем данные из формы
    name = request.form.get("name")
    description = request.form.get("description")

    if not name:
        flash("Название проекта обязательно", "danger")
        return redirect(url_for("project_select"))

    # создаём проект
    project = Project(name=name, description=description)
    db.session.add(project)
    db.session.commit()

    # автоматически добавляем текущего пользователя в проект с ролью ADMIN
    db.session.execute(user_project.insert().values(
        user_id=current_user.id,
        project_id=project.id,
        role=Role.ADMIN
    ))
    db.session.commit()

    flash(f"Проект '{name}' создан. Вы администратор проекта.", "success")
    return redirect(url_for("project_select"))


def role_required_for_project(required_role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # пытаемся получить project_id из URL или формы
            project_id = kwargs.get('project_id') or request.form.get('project_id')
            project = Project.query.get(project_id)
            if not project:
                flash("Проект не найден", "danger")
                return redirect(url_for('dashboard'))

            role = get_user_role(current_user, project)
            if role != required_role:
                flash("Доступ запрещён", "danger")
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('project_select'))
        flash("Неверный email или пароль", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/project/<int:project_id>/UsersList')
@login_required
def project_users_list(project_id):

    project = Project.query.get_or_404(project_id)

    role = get_user_role(current_user, project)

    if role != Role.ADMIN:
        flash("Только администратор может управлять пользователями", "danger")
        return redirect(url_for("project_dashboard", project_id=project.id))

    users_data = []

    for user in project.users:
        user_role = db.session.execute(
            user_project.select().where(
                (user_project.c.user_id == user.id) &
                (user_project.c.project_id == project.id)
            )
        ).first()

        users_data.append({
            "user": user,
            "role": user_role.role
        })

    print("=== DEBUG USERS LIST ===")
    print("Project:", project.id, project.name)
    print("Current user:", current_user.id, current_user.username)
    print("Current role:", role)

    print("Users in project:")
    for u in users_data:
        print("User:", u["user"].id, u["user"].username, "| Role:", u["role"])

    print("Total users:", len(users_data))
    print("========================")


    return render_template(
        "users_list.html",
        project=project,
        users=users_data,
        role=role
    )


@app.route('/project/<int:project_id>/add_user', methods=['POST'])
@login_required
def add_user(project_id):

    project = Project.query.get_or_404(project_id)

    role = get_user_role(current_user, project)

    if role != Role.ADMIN:
        flash("Нет доступа", "danger")
        return redirect(url_for("project_users_list", project_id=project.id))

    email = request.form.get("email")

    user = User.query.filter_by(email=email).first()

    if not user:
        flash("Пользователь не найден", "danger")
        return redirect(url_for("project_users_list", project_id=project.id))

    existing = db.session.execute(
        user_project.select().where(
            (user_project.c.user_id == user.id) &
            (user_project.c.project_id == project.id)
        )
    ).first()

    if existing:
        flash("Пользователь уже в проекте", "warning")
        return redirect(url_for("project_users_list", project_id=project.id))

    db.session.execute(user_project.insert().values(
        user_id=user.id,
        project_id=project.id,
        role=Role.ANNOTATOR
    ))

    db.session.commit()

    flash("Пользователь добавлен", "success")

    return redirect(url_for("project_users_list", project_id=project.id))







# ======== Роут проекта ========
# @app.route('/project/<int:project_id>')
# @login_required
# def project_dashboard(project_id):
#     # Получаем проект
#     project = Project.query.get_or_404(project_id)
#
#     # Проверяем, что пользователь участвует в проекте
#     role = get_user_role(current_user, project)
#     if not role:
#         flash("У вас нет доступа к этому проекту", "danger")
#         return redirect(url_for('project_select'))
#
#     # ===== Пользователи =====
#     if role == Role.ADMIN:
#         users = project.users  # показываем всех пользователей проекта
#     else:
#         #users = []  # анотаторы видят только себя / минимальные данные
#         users = project.users
#
#     # ===== Media (изображения проекта) =====
#     images = Image.query.filter_by(project_id=project.id).all()
#
#     # ===== Статистика для проекта =====
#     total_images = len(images)
#     total_annotations = sum(len(img.annotations) for img in images)
#
#     return render_template(
#         "dashboard.html",
#         projects=[project],   # передаём список с одним проектом для совместимости шаблона
#         users=users,
#         media_files=[img.filename for img in images],
#         role=role,
#         project=project,
#         total_images=total_images,
#         total_annotations=total_annotations
#     )
#
#





@app.route('/project/<int:project_id>/export')
@login_required
def export_annotations(project_id):
    import json, csv, io, zipfile
    from flask import send_file

    project = Project.query.get_or_404(project_id)

    if get_user_role(current_user, project) != Role.ADMIN:
        flash("Нет доступа", "danger")
        return redirect(url_for('project_media', project_id=project_id))

    fmt = request.args.get('format', 'json')        # json / csv / coco / yolo
    include_images = request.args.get('images', 'false') == 'true'

    images = Image.query.filter_by(project_id=project_id).all()

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:

        # ── Фотографии (опционально) ──
        if include_images:
            for img in images:
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], img.filename)
                if os.path.exists(filepath):
                    zf.write(filepath, f"images/{img.filename}")

        # ── JSON ──
        if fmt == 'json':
            data = []
            for img in images:
                data.append({
                    "image_id": img.id,
                    "filename": img.filename,
                    "annotations": [
                        {"x": a.x, "y": a.y, "width": a.width, "height": a.height, "label": a.label}
                        for a in img.annotations
                    ]
                })
            zf.writestr("annotations.json", json.dumps(data, ensure_ascii=False, indent=2))

        # ── CSV ──
        elif fmt == 'csv':
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["image_id", "filename", "label", "x", "y", "width", "height"])
            for img in images:
                for a in img.annotations:
                    writer.writerow([img.id, img.filename, a.label, a.x, a.y, a.width, a.height])
            zf.writestr("annotations.csv", output.getvalue())

        # ── COCO JSON ──
        elif fmt == 'coco':
            categories = {}
            coco_images = []
            coco_annotations = []
            ann_id = 1

            for img in images:
                coco_images.append({
                    "id": img.id,
                    "file_name": img.filename,
                    "width": None,   # можно добавить реальные размеры через Pillow
                    "height": None
                })
                for a in img.annotations:
                    if a.label not in categories:
                        categories[a.label] = len(categories) + 1
                    coco_annotations.append({
                        "id": ann_id,
                        "image_id": img.id,
                        "category_id": categories[a.label],
                        "bbox": [a.x, a.y, a.width, a.height],  # COCO: [x, y, w, h]
                        "area": a.width * a.height,
                        "iscrowd": 0
                    })
                    ann_id += 1

            coco = {
                "images": coco_images,
                "annotations": coco_annotations,
                "categories": [{"id": v, "name": k} for k, v in categories.items()]
            }
            zf.writestr("annotations_coco.json", json.dumps(coco, ensure_ascii=False, indent=2))

        # ── YOLO TXT ──
        elif fmt == 'yolo':
            # Собираем все уникальные метки
            all_labels = sorted(set(
                a.label for img in images for a in img.annotations
            ))
            label_to_id = {l: i for i, l in enumerate(all_labels)}

            # classes.txt
            zf.writestr("classes.txt", "\n".join(all_labels))

            for img in images:
                if not img.annotations:
                    continue

                # Нужны реальные размеры изображения для нормализации
                try:
                    from PIL import Image as PILImage
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], img.filename)
                    with PILImage.open(filepath) as pil_img:
                        img_w, img_h = pil_img.size
                except Exception:
                    img_w, img_h = 1, 1  # fallback

                lines = []
                for a in img.annotations:
                    cls = label_to_id[a.label]
                    # YOLO: center_x center_y width height (нормализованные 0..1)
                    cx = (a.x + a.width / 2) / img_w
                    cy = (a.y + a.height / 2) / img_h
                    w  = a.width / img_w
                    h  = a.height / img_h
                    lines.append(f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")

                # Имя файла без расширения
                base = os.path.splitext(img.filename)[0]
                zf.writestr(f"labels/{base}.txt", "\n".join(lines))

    zip_buffer.seek(0)
    zip_name = f"{project.name}_annotations_{fmt}.zip"

    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_name
    )





@app.route('/save_annotation', methods=['POST'])
@login_required
def save_annotation():
    data = request.json
    ann = Annotation(
        image_id = data['image_id'],
        x = data['x'],
        y = data['y'],
        width = data['width'],
        height = data['height'],
        label = data['label'],
        created_by = current_user.id
    )
    db.session.add(ann)
    db.session.commit()
    return jsonify({"status":"success"})


@app.route('/project/<int:project_id>/add_user_to_project', methods=['POST'])
@login_required
def add_user_to_project(project_id):
    data = request.get_json()
    user_id = data.get("user_id")
    role = data.get("role", Role.ANNOTATOR)

    project = Project.query.get_or_404(project_id)

    if get_user_role(current_user, project) != Role.ADMIN:
        return jsonify({"success": False, "error": "Нет доступа"})

    user = User.query.get(user_id)
    if not user:
        return jsonify({"success": False, "error": "Пользователь не найден"})

    existing = db.session.execute(
        user_project.select().where(
            (user_project.c.user_id == user.id) &
            (user_project.c.project_id == project.id)
        )
    ).first()
    if existing:
        return jsonify({"success": False, "error": "Пользователь уже в проекте"})

    db.session.execute(user_project.insert().values(
        user_id=user.id,
        project_id=project.id,
        role=role
    ))
    db.session.commit()

    return jsonify({"success": True, "user": {"id": user.id, "username": user.username, "role": role}})


@app.route('/project/<int:project_id>/media')
@login_required
def project_media(project_id):
    project = Project.query.get_or_404(project_id)

    role = get_user_role(current_user, project)
    if not role:
        flash("Нет доступа к проекту", "danger")
        return redirect(url_for('project_select'))

    images = Image.query.filter_by(project_id=project.id).all()

    total_annotations = sum(len(img.annotations) for img in images)
    annotated_count = sum(1 for img in images if len(img.annotations) > 0)

    return render_template(
        "media.html",
        project=project,
        images=images,
        role=role,
        total_annotations=total_annotations,
        annotated_count=annotated_count
    )


@app.route('/project/<int:project_id>/upload_image', methods=['POST'])
@login_required
def upload_image(project_id):
    project = Project.query.get_or_404(project_id)

    role = get_user_role(current_user, project)
    if role != Role.ADMIN:
        flash("Только администратор может загружать изображения", "danger")
        return redirect(url_for('project_media', project_id=project_id))

    file = request.files.get('image')
    if not file or file.filename == '':
        flash("Файл не выбран", "danger")
        return redirect(url_for('project_media', project_id=project_id))

    # Безопасное имя файла
    from werkzeug.utils import secure_filename
    import uuid
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
    if ext not in allowed:
        flash("Неподдерживаемый формат файла", "danger")
        return redirect(url_for('project_media', project_id=project_id))

    filename = secure_filename(file.filename)
    # Добавляем uuid чтобы избежать коллизий имён
    filename = f"{uuid.uuid4().hex}_{filename}"

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    img = Image(filename=filename, project_id=project_id)
    db.session.add(img)
    db.session.commit()

    flash("Изображение загружено", "success")
    return redirect(url_for('project_media', project_id=project_id))


@app.route('/image/<int:image_id>/delete', methods=['POST'])
@login_required
def delete_image(image_id):
    image = Image.query.get_or_404(image_id)
    project_id = image.project_id

    role = get_user_role(current_user, image.project)
    if role != Role.ADMIN:
        flash("Нет доступа", "danger")
        return redirect(url_for('project_media', project_id=project_id))

    # Сначала удаляем аннотации вручную
    Annotation.query.filter_by(image_id=image_id).delete()

    # Удаляем файл с диска
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], image.filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    db.session.delete(image)
    db.session.commit()

    flash("Изображение удалено", "success")
    return redirect(url_for('project_media', project_id=project_id))


@app.route('/annotate/<int:image_id>')
@login_required
def annotate(image_id):
    image = Image.query.get_or_404(image_id)

    role = get_user_role(current_user, image.project)
    if not role:
        flash("Нет доступа", "danger")
        return redirect(url_for('project_select'))

    # Соседние изображения для навигации
    all_images = Image.query.filter_by(project_id=image.project_id).order_by(Image.id).all()
    ids = [img.id for img in all_images]
    idx = ids.index(image_id)

    prev_image = all_images[idx - 1] if idx > 0 else None
    next_image = all_images[idx + 1] if idx < len(all_images) - 1 else None

    # Существующие аннотации для этого изображения
    import json
    annotations_json = json.dumps([
        {
            "x": a.x, "y": a.y,
            "width": a.width, "height": a.height,
            "label": a.label
        }
        for a in image.annotations
    ])

    return render_template(
        'annotate.html',
        image=image,
        prev_image=prev_image,
        next_image=next_image,
        annotations_json=annotations_json
    )


@app.route('/save_annotations', methods=['POST'])
@login_required
def save_annotations():
    data = request.get_json()
    image_id = data.get('image_id')
    new_annotations = data.get('annotations', [])

    image = Image.query.get(image_id)
    if not image:
        return jsonify({"status": "error", "message": "Изображение не найдено"})

    role = get_user_role(current_user, image.project)
    if not role:
        return jsonify({"status": "error", "message": "Нет доступа"})

    # Удаляем старые аннотации этого пользователя для данного изображения
    Annotation.query.filter_by(image_id=image_id, created_by=current_user.id).delete()

    # Сохраняем новые
    for a in new_annotations:
        ann = Annotation(
            image_id=image_id,
            x=a.get('x'), y=a.get('y'),
            width=a.get('width'), height=a.get('height'),
            label=a.get('label', ''),
            created_by=current_user.id
        )
        db.session.add(ann)

    db.session.commit()
    return jsonify({"status": "success", "saved": len(new_annotations)})

@app.route('/project/<int:project_id>/remove_user_from_project', methods=['POST'])
@login_required
def remove_user_from_project(project_id):
    data = request.get_json()
    user_id = data.get("user_id")

    project = Project.query.get_or_404(project_id)

    if get_user_role(current_user, project) != Role.ADMIN:
        return jsonify({"success": False, "error": "Нет доступа"})

    if int(user_id) == current_user.id:
        return jsonify({"success": False, "error": "Нельзя удалить самого себя"})

    db.session.execute(
        user_project.delete().where(
            (user_project.c.user_id == user_id) &
            (user_project.c.project_id == project_id)
        )
    )
    db.session.commit()
    return jsonify({"success": True})


@app.route('/project/<int:project_id>/update_user_role', methods=['POST'])
@login_required
def update_user_role(project_id):
    data = request.get_json()
    user_id = data.get("user_id")
    new_role = data.get("role")

    project = Project.query.get_or_404(project_id)

    if get_user_role(current_user, project) != Role.ADMIN:
        return jsonify({"success": False, "error": "Нет доступа"})
    if int(user_id) == current_user.id:
        return jsonify({"success": False, "error": "Нельзя изменить свою роль"})

    db.session.execute(
        user_project.update().where(
            (user_project.c.user_id == user_id) &
            (user_project.c.project_id == project_id)
        ).values(role=new_role)
    )
    db.session.commit()
    return jsonify({"success": True})



def create_default_admin():
    admin = User.query.filter_by(email="admin@admin.com").first()

    if not admin:
        admin = User(
            username="admin",
            email="admin@admin.com",
            password=generate_password_hash("admin"),
        )

        db.session.add(admin)
        db.session.commit()

        print("✅ Админ создан")
        print("login: admin@admin.com")
        print("password: admin123")


with app.app_context():
    db.create_all()
    create_default_admin()



#---TEST ROUTE

@app.route('/project/<int:project_id>')
@login_required
def project_dashboard(project_id):
    """Тестовый дашборд с правильной статистикой"""
    from datetime import datetime, timedelta
    from sqlalchemy import func

    # Получаем проект
    project = Project.query.get_or_404(project_id)

    # Проверяем доступ
    role = get_user_role(current_user, project)
    if not role:
        flash("У вас нет доступа к этому проекту", "danger")
        return redirect(url_for('project_select'))

    # ========== 1. БАЗОВАЯ СТАТИСТИКА ==========
    images = Image.query.filter_by(project_id=project.id).all()
    total_images = len(images)

    # СЧИТАЕМ ПРАВИЛЬНО:
    # - annotated_files - количество файлов, у которых есть хотя бы одна аннотация
    # - total_annotations - общее количество аннотаций (объектов)
    annotated_files = 0
    total_annotations = 0
    file_annotations_map = {}  # словарь: image_id -> количество аннотаций

    for img in images:
        ann_count = Annotation.query.filter_by(image_id=img.id).count()
        total_annotations += ann_count
        file_annotations_map[img.id] = ann_count
        if ann_count > 0:
            annotated_files += 1

    # Процент размеченных файлов (НЕ аннотаций!)
    annotated_percentage = (annotated_files / total_images * 100) if total_images > 0 else 0

    # Подсчёт типов файлов
    images_count = sum(1 for img in images if img.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')))
    videos_count = sum(1 for img in images if img.filename.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')))

    # ========== 2. СТАТИСТИКА ПО ПОЛЬЗОВАТЕЛЯМ ==========
    users = project.users

    # Подготавливаем данные о пользователях с их ролями
    users_with_roles = []
    admins_count = 0
    annotators_count = 0
    user_annotations_map = {}  # количество АННОТАЦИЙ пользователя
    user_files_map = {}  # количество ФАЙЛОВ, которые пользователь разметил

    for user in users:
        user_role = get_user_role(user, project)
        users_with_roles.append({
            'user': user,
            'role': user_role
        })

        if user_role == Role.ADMIN:
            admins_count += 1
        else:
            annotators_count += 1

        # Считаем аннотации пользователя (объекты)
        user_ann_count = Annotation.query.filter(
            Annotation.created_by == user.id,
            Annotation.image_id.in_([img.id for img in images])
        ).count()
        user_annotations_map[user.id] = user_ann_count

        # Считаем количество РАЗНЫХ файлов, которые пользователь разметил
        user_files = db.session.query(Annotation.image_id).filter(
            Annotation.created_by == user.id,
            Annotation.image_id.in_([img.id for img in images])
        ).distinct().count()
        user_files_map[user.id] = user_files

    # Количество пользователей, которые сделали хотя бы одну аннотацию
    active_users = len([u for u in users if user_annotations_map.get(u.id, 0) > 0])

    # ========== 3. ЛЕНТА АКТИВНОСТИ ==========
    recent_activities = []

    # Последние 5 загруженных изображений
    recent_images = Image.query.filter_by(project_id=project.id) \
        .order_by(Image.id.desc()).limit(5).all()

    for img in recent_images:
        uploader = users[0] if users else None
        if uploader:
            recent_activities.append({
                'user_name': uploader.username,
                'action': 'загрузил(а) новый файл',
                'details': img.filename,
                'icon': 'fa-upload',
                'time_ago': 'только что'
            })

    # Последние 5 аннотаций
    last_annotations = Annotation.query.filter(
        Annotation.image_id.in_([img.id for img in images])
    ).order_by(Annotation.id.desc()).limit(5).all()

    for ann in last_annotations:
        user = User.query.get(ann.created_by)
        img = Image.query.get(ann.image_id)
        if user and img:
            recent_activities.append({
                'user_name': user.username,
                'action': f'разметил(а) объект "{ann.label}"',
                'details': f'на {img.filename}',
                'icon': 'fa-tag',
                'time_ago': 'недавно'
            })

    recent_activities = recent_activities[:10]

    # Последняя активность проекта
    last_activity_date = '—'
    if last_annotations:
        last_activity_date = 'сегодня'
    elif recent_images:
        last_activity_date = 'сегодня'

    # ========== 4. ПОСЛЕДНИЕ МЕДИАФАЙЛЫ ==========
    recent_media = []
    for img in images[:6]:
        is_annotated = Annotation.query.filter_by(image_id=img.id).count() > 0
        ann_count = Annotation.query.filter_by(image_id=img.id).count()

        recent_media.append({
            'filename': img.filename,
            'file_type': 'image' if not img.filename.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')) else 'video',
            'is_annotated': is_annotated,
            'annotations_count': ann_count,
            'id': img.id
        })

    # ========== 5. СТАТИСТИКА ПО МЕТКАМ ==========
    labels_stats = db.session.query(
        Annotation.label,
        func.count(Annotation.id).label('count')
    ).join(Image, Annotation.image_id == Image.id) \
        .filter(Image.project_id == project.id) \
        .group_by(Annotation.label) \
        .all()

    top_labels = [{'label': l[0] if l[0] else 'без метки', 'count': l[1]} for l in labels_stats[:5]]

    # ========== 6. СТАТИСТИКА ДЛЯ ГРАФИКА (распределение аннотаций по пользователям) ==========
    user_labels_chart = []
    for user in users:
        user_anns = user_annotations_map.get(user.id, 0)
        if user_anns > 0:
            user_labels_chart.append({
                'username': user.username,
                'count': user_anns
            })
    user_labels_chart = sorted(user_labels_chart, key=lambda x: x['count'], reverse=True)[:5]

    # ========== 7. ДАННЫЕ ДЛЯ ШАБЛОНА ==========
    context = {
        'project': project,
        'role': role,
        'current_user': current_user,

        # Метрики
        'total_images': total_images,
        'images_count': images_count,
        'videos_count': videos_count,
        'total_annotations': total_annotations,
        'annotated_files': annotated_files,
        'annotated_percentage': annotated_percentage,
        'users_with_roles': users_with_roles,
        'admins_count': admins_count,
        'annotators_count': annotators_count,
        'active_users': active_users,

        # Прогресс пользователей
        'user_annotations_map': user_annotations_map,
        'user_files_map': user_files_map,

        # Активность
        'recent_activities': recent_activities,
        'last_activity_date': last_activity_date,

        # Медиа
        'recent_media': recent_media,
        'file_annotations_map': file_annotations_map,

        # Графики
        'top_labels': top_labels,
        'user_labels_chart': user_labels_chart,
    }

    return render_template("dashboard.html", **context)
# ======== Запуск ========
if __name__ == "__main__":
    with app.app_context():  # <-- создаём контекст приложения
        db.create_all()
    app.run(debug=True)