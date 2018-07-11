from django.conf.urls import url

from . import views

app_name = 'locations'
urlpatterns = [
    url(r'^cartodbtables/$', views.CartoDBTablesView.as_view(), name='cartodbtables'),
    url(r'^autocomplete/$', views.LocationQuerySetView.as_view(), name='locations_autocomplete'),
]
