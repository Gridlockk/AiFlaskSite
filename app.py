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

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ======== Роли ========
class Role:
    ADMIN = 'admin'
    ANNOTATOR = 'annotator'

# ======== Модели ========
user_project = db.Table('user_project',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('project_id', db.Integer, db.ForeignKey('project.id'))
)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    team = db.Column(db.String(100), default="Моя команда")
    role = db.Column(db.String(20), default=Role.ANNOTATOR)
    projects = db.relationship('Project', secondary=user_project, back_populates='users')
    annotations = db.relationship('Annotation', backref='creator', lazy=True)

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    users = db.relationship('User', secondary=user_project, back_populates='projects')
    images = db.relationship('Image', backref='project', lazy=True)

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

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash("Неверный email или пароль", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ======== Дашборд ========
@app.route('/dashboard')
@login_required
def dashboard():

    # ===== Проекты =====
    if current_user.role == Role.ADMIN:
        projects = Project.query.all()
        users = User.query.all()
    else:
        projects = current_user.projects
        users = []

    # ===== Media (файлы из папки uploads) =====
    media_files = []

    media_path = app.config['UPLOAD_FOLDER']

    if os.path.exists(media_path):
        for file in os.listdir(media_path):

            # фильтр изображений
            if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.mp4')):
                media_files.append(file)

    return render_template(
        "dashboard.html",
        projects=projects,
        users=users,
        media_files=media_files,
        role=current_user.role
    )

# ======== Создание проекта (только админ) ========


# ======== Загрузка изображения (только админ) ========
@app.route('/upload_image/<int:project_id>', methods=['POST'])
@login_required
@role_required(Role.ADMIN)
def upload_image(project_id):
    file = request.files['image']
    if file:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(filepath)
        img = Image(filename=file.filename, project_id=project_id)
        db.session.add(img)
        db.session.commit()
        flash("Изображение загружено", "success")
    return redirect(url_for('dashboard'))

# ======== Разметка ========
@app.route('/annotate/<int:image_id>')
@login_required
def annotate(image_id):
    image = Image.query.get_or_404(image_id)
    return render_template('annotate.html', image=image)


@app.route('/media')
@login_required
def media():

    # берём первый проект пользователя
    project = current_user.projects[0] if current_user.projects else None

    if not project:
        flash("У вас нет проектов", "warning")
        return redirect(url_for('dashboard'))

    images = Image.query.filter_by(project_id=project.id).all()

    return render_template(
        "media.html",
        images=images,
        project=project
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


@app.route('/add_user_to_project', methods=['POST'])
@login_required
@role_required(Role.ADMIN)
def add_user_to_project():

    user_id = request.form.get("user_id")
    project_id = request.form.get("project_id")

    user = User.query.get(user_id)
    project = Project.query.get(project_id)

    if user and project:
        project.users.append(user)
        db.session.commit()
        flash("Пользователь добавлен в проект", "success")

    return redirect(url_for("dashboard"))

@app.route('/create_project', methods=['POST'])
@login_required
@role_required(Role.ADMIN)
def create_project():

    name = request.form.get("name")
    description = request.form.get("description")

    project = Project(
        name=name,
        description=description
    )

    db.session.add(project)
    db.session.commit()

    flash("Проект создан", "success")

    return redirect(url_for("dashboard"))

def create_default_admin():
    admin = User.query.filter_by(email="admin@admin.com").first()

    if not admin:
        admin = User(
            username="Admin",
            email="admin@admin.com",
            password=generate_password_hash("admin"),
            role=Role.ADMIN
        )

        db.session.add(admin)
        db.session.commit()

        print("✅ Админ создан")
        print("login: admin@admin.com")
        print("password: admin123")


with app.app_context():
    db.create_all()
    create_default_admin()

# ======== Запуск ========
if __name__ == "__main__":
    with app.app_context():  # <-- создаём контекст приложения
        db.create_all()
    app.run(debug=True)