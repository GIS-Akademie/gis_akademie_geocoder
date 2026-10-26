# -*- coding: utf-8 -*-

def classFactory(iface):
    from .gis_akademie_geocoder_plugin import GISAkademieGeocoderPlugin
    return GISAkademieGeocoderPlugin(iface)
