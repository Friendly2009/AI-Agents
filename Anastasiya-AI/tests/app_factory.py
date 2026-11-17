from application import Application
from storage import Store
from manager import Manager
from family_radar import FamilyRadar
from sales_learning import SalesLearning

created = []

def make_app():
    app = Application(Store(':memory:'), Manager(':memory:'), FamilyRadar(':memory:'), SalesLearning(':memory:'))
    created.append(app)
    return app
