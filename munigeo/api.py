import re
import json
from django.conf import settings
from django.db.models import Q
from django.contrib.gis.measure import D
from django.contrib.gis.db import models
from rest_framework import serializers, pagination, relations, viewsets, generics
from rest_framework.exceptions import ParseError
from django.contrib.gis.gdal import SRSException, CoordTransform, SpatialReference
from django.contrib.gis.geos import Point, Polygon
from django.contrib.gis.geos.base import gdal
from munigeo.models import AdministrativeDivisionType, AdministrativeDivision,\
    Municipality, POICategory, POI, Plan, Street, Address
from modeltranslation import models as mt_models # workaround for init problem
from modeltranslation.translator import translator, NotRegistered

# Use the GPS coordinate system by default
DEFAULT_SRID = 4326


all_views = []
def register_view(klass, name):
    all_views.append({'class': klass, 'name': name})

def poly_from_bbox(bbox_val):
    points = bbox_val.split(',')
    if len(points) != 4:
        raise ParseError("bbox must be in format 'left,bottom,right,top'")
    try:
        points = [float(p) for p in points]
    except ValueError:
        raise ParseError("bbox values must be floating points or integers")
    poly = Polygon.from_bbox(points)
    return poly

def srid_to_srs(srid):
    if not srid:
        srid = DEFAULT_SRID
    try:
        srid = int(srid)
    except ValueError:
        raise ParseError("'srid' must be an integer")
    try:
        srs = SpatialReference(srid)
    except SRSException:
        raise ParseError("SRID %d not found (try 4326 for GPS coordinate system)" % srid)
    return srs

def build_bbox_filter(srs, bbox_val, field_name):
    poly = poly_from_bbox(bbox_val)
    poly.set_srid(srs.srid)

    return {"%s__within" % field_name: poly}

def make_muni_ocd_id(name, rest=None):
    country = getattr(settings, 'DEFAULT_COUNTRY', None)
    muni = getattr(settings, 'DEFAULT_OCD_MUNICIPALITY', None)
    if country and muni:
        s = 'ocd-division/country:%s/%s:%s' % (settings.DEFAULT_COUNTRY, settings.DEFAULT_OCD_MUNICIPALITY, name)
    else:
        s = name
    if rest:
        s += '/' + rest
    return s


class TranslatedModelSerializer(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        super(TranslatedModelSerializer, self).__init__(*args, **kwargs)
        model = self.opts.model
        try:
            trans_opts = translator.get_options_for_model(model)
        except NotRegistered:
            self.translated_fields = []
            return

        self.translated_fields = trans_opts.fields.keys()
        lang_codes = [x[0] for x in settings.LANGUAGES]
        # Remove the pre-existing data in the bundle.
        for field_name in self.translated_fields:
            for lang in lang_codes:
                key = "%s_%s" % (field_name, lang)
                if key in self.fields:
                    del self.fields[key]
            del self.fields[field_name]

    def to_native(self, obj):
        ret = super(TranslatedModelSerializer, self).to_native(obj)
        if obj is None:
            return ret
        return self.translated_fields_to_native(obj, ret)

    def translated_fields_to_native(self, obj, ret):
        for field_name in self.translated_fields:
            d = {}
            default_lang = settings.LANGUAGES[0][0]
            d[default_lang] = getattr(obj, field_name)
            for lang in [x[0] for x in settings.LANGUAGES[1:]]:
                key = "%s_%s" % (field_name, lang)  
                val = getattr(obj, key, None)
                if val == None:
                    continue 
                d[lang] = val

            # If no text provided, leave the field as null
            for key, val in d.items():
                if val != None:
                    break
            else:
                d = None
            ret[field_name] = d

        return ret


class MPTTModelSerializer(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        super(MPTTModelSerializer, self).__init__(*args, **kwargs)
        for field_name in 'lft', 'rght', 'tree_id', 'level':
            if field_name in self.fields:
                del self.fields[field_name]


# Maintain a cache of different coordinate transformations for performance
# reasons.
srs_cache = {}
coord_transforms = {}

def geom_to_json(geom, target_srs):
    srs = srs_cache.get(geom.srid, None)
    if not srs:
        srs = geom.srs
        srs_cache[geom.srid] = srs

    if target_srs:
        ct_id = '%s-%s' % (geom.srid, target_srs.srid)
        ct = coord_transforms.get(ct_id, None)
        if not ct:
            ct = CoordTransform(srs, target_srs)
            coord_transforms[ct_id] = ct
    else:
        ct = None

    if ct:
        wkb = geom.wkb
        geom = gdal.OGRGeometry(wkb, srs)
        geom.transform(ct)
        geom_name = geom.geom_name.lower()
    else:
        geom_name = geom.geom_type.lower()

    # Accelerated path for points
    if geom_name == 'point':
        if target_srs.projected:
            digits = 2
        else:
            digits = 7
        coords = [round(n, digits) for n in [geom.x, geom.y]]
        return {'type': 'Point', 'coordinates': coords}

    s = geom.geojson
    return json.loads(s)


class GeoModelSerializer(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        super(GeoModelSerializer, self).__init__(*args, **kwargs)
        model = self.opts.model
        self.geo_fields = []
        model_fields = [f.name for f in model._meta.fields]
        for field_name in self.fields:
            if not field_name in model_fields:
                continue
            field = model._meta.get_field(field_name)
            if not isinstance(field, models.GeometryField):
                continue
            self.geo_fields.append(field_name)
            del self.fields[field_name]

    def to_native(self, obj):
        # SRS is deduced in ViewSet and passed from there
        srs = self.context.get('srs', None)
        ret = super(GeoModelSerializer, self).to_native(obj)
        if obj is None:
            return ret
        for field_name in self.geo_fields:
            val = getattr(obj, field_name)
            if val == None:
                ret[field_name] = None
                continue
            ret[field_name] = geom_to_json(val, srs)
        return ret

class GeoModelAPIView(generics.GenericAPIView):
    def initial(self, request, *args, **kwargs):
        super(GeoModelAPIView, self).initial(request, *args, **kwargs)
        srid = request.QUERY_PARAMS.get('srid', None)
        self.srs = srid_to_srs(srid)

    def get_serializer_context(self):
        ret = super(GeoModelAPIView, self).get_serializer_context()
        ret['srs'] = self.srs
        return ret



class AdministrativeDivisionTypeSerializer(TranslatedModelSerializer):
    class Meta:
        model = AdministrativeDivisionType

class AdministrativeDivisionTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AdministrativeDivisionType.objects.all()
    serializer_class = AdministrativeDivisionTypeSerializer

register_view(AdministrativeDivisionTypeViewSet, 'administrative_division_type')



class AdministrativeDivisionSerializer(GeoModelSerializer, TranslatedModelSerializer,
                                       MPTTModelSerializer):
    def to_native(self, obj):
        ret = super(AdministrativeDivisionSerializer, self).to_native(obj)
        if not 'request' in self.context:
            return ret
        qparams = self.context['request'].QUERY_PARAMS
        if qparams.get('geometry', '').lower() in ('true', '1'):
            geom = obj.geometry.boundary
            if self.srs.srid != geom.srid:
                ct = CoordTransform(geom.srs, self.srs)
                geom.transform(ct)
            geom_str = geom.geojson
            ret['boundary'] = json.loads(geom_str)
        return ret

    class Meta:
        model = AdministrativeDivision

class AdministrativeDivisionViewSet(GeoModelAPIView, viewsets.ReadOnlyModelViewSet):
    queryset = AdministrativeDivision.objects.all()
    serializer_class = AdministrativeDivisionSerializer

    def get_queryset(self):
        queryset = super(AdministrativeDivisionViewSet, self).get_queryset()
        filters = self.request.QUERY_PARAMS

        if 'type' in filters:
            type_str = filters['type'].strip()
            # If the given type is not digits, assume it's a type name
            if not re.match(r'^[\d]+$', type_str):
                queryset = queryset.filter(type__type=type_str)
            else:
                queryset = queryset.filter(type=type_str)

        if 'input' in filters:
            queryset = queryset.filter(name__icontains=filters['input'].strip())

        if 'ocd_id' in filters:
            # Divisions can be specified with form:
            # division=helsinki/kaupunginosa:kallio,vantaa/äänestysalue:5
            d_list = filters['ocd_id'].lower().split(',')
            ocd_id_list = []
            for division_path in d_list:
                if division_path.startswith('ocd-division'):
                    muni_ocd_id = division_path
                else:
                    ocd_id_base = r'[\w0-9~_.-]+'
                    match_re = r'(%s)/([\w_-]+):(%s)' % (ocd_id_base, ocd_id_base)
                    m = re.match(match_re, division_path, re.U)
                    if not m:
                        raise ParseError("'ocd_id' must be of form 'muni/type:id'")

                    arr = division_path.split('/')
                    muni_ocd_id = make_muni_ocd_id(arr.pop(0), '/'.join(arr))
                ocd_id_list.append(muni_ocd_id)

            queryset = queryset.filter(ocd_id__in=ocd_id_list)


        return queryset

register_view(AdministrativeDivisionViewSet, 'administrative_division')


class AddressSerializer(GeoModelSerializer):
    def to_native(self, obj):
        ret = super(AddressSerializer, self).to_native(obj)
        if not ret['number_end']:
            ret['number_end'] = None
        if not ret['letter']:
            ret['letter'] = None
        return ret

    class Meta:
        model = Address
        exclude = ('id', 'street')


class StreetSerializer(TranslatedModelSerializer):
    addresses = AddressSerializer(many=True)

    class Meta:
        model = Street

LANG_CODES = [x[0] for x in settings.LANGUAGES]

class StreetViewSet(GeoModelAPIView, viewsets.ReadOnlyModelViewSet):
    queryset = Street.objects.all()
    serializer_class = StreetSerializer

    def get_queryset(self):
        queryset = super(StreetViewSet, self).get_queryset()
        default_lang = LANG_CODES[0]
        self.lang_code = self.request.QUERY_PARAMS.get('language', default_lang)
        if self.lang_code not in LANG_CODES[0]:
            raise ParseError("Invalid language supplied. Supported languages: %s" %
                             ','.join([x[0] for x in settings.LANGUAGES]))

        filters = self.request.QUERY_PARAMS

        if 'municipality' in filters:
            val = filters['municipality'].lower()
            if val.startswith('ocd-division'):
                ocd_id = val
            else:
                ocd_id = make_muni_ocd_id(val)
            try:
                muni = Municipality.objects.get(ocd_id=ocd_id)
            except Municipality.DoesNotExist:
                raise ParseError("municipality with ID '%s' not found" % ocd_id)

            queryset = queryset.filter(location__within=muni.geometry.boundary)

        if 'input' in filters:
            args = {}
            args['name_%s__istartswith' % self.lang_code] = filters['input'].strip()
            queryset = queryset.filter(**args)

        return queryset

register_view(StreetViewSet, 'street')


class MunicipalitySerializer(TranslatedModelSerializer):
    class Meta:
        model = Municipality
