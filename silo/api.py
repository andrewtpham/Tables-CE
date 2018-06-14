import json
import django_filters
from urlparse import urljoin

from django.http import HttpResponseBadRequest, JsonResponse, HttpResponse
from django.db.models import Q
from django.conf import settings
from django.shortcuts import get_object_or_404

from rest_framework import viewsets, filters, permissions
from rest_framework.decorators import (detail_route, list_route, api_view,
                                       authentication_classes,
                                       permission_classes)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_json_api.parsers import JSONParser
from rest_framework_json_api.renderers import JSONRenderer
from rest_framework import mixins, status

from .serializers import *
from .models import (Silo, LabelValueStore, Country, WorkflowLevel1,
                     WorkflowLevel2, TolaUser, Read, ReadType)
from silo.permissions import *
from tola.util import (getSiloColumnNames, getCompleteSiloColumnNames,
                       save_data_to_silo, JSONEncoder)


class TolaUserViewSet(viewsets.ModelViewSet):
    """
    A ViewSet for listing or retrieving TolaUsers.
    """
    def list(self, request):
        # Use this queryset or the django-filters lib will not work
        queryset = self.filter_queryset(self.get_queryset())
        if not request.user.is_superuser:
            organization_id = TolaUser.objects.\
                values_list('organization_id', flat=True).\
                get(user=request.user)
            queryset = queryset.filter(organization_id=organization_id)
        serializer = TolaUserSerializer(
            instance=queryset, context={'request': request}, many=True)
        return Response(serializer.data)

    def retrieve(self, request, pk=None):
        queryset = self.queryset
        user = get_object_or_404(queryset, pk=pk)
        serializer = TolaUserSerializer(instance=user,
                                        context={'request': request})
        return Response(serializer.data)

    filter_fields = ('organization__id', 'tola_user_uuid')
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    queryset = TolaUser.objects.all()
    serializer_class = TolaUserSerializer


class OrganizationViewSet(viewsets.ModelViewSet):
    filter_fields = ('name', 'organization_uuid')
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer


class UserViewSet(viewsets.ReadOnlyModelViewSet):

    queryset = User.objects.all()
    serializer_class = UserSerializer


class CountryViewSet(viewsets.ModelViewSet):
    filter_fields = ('country',)
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    queryset = Country.objects.all()
    serializer_class = CountrySerializer


class WorkflowLevel1ViewSet(viewsets.ModelViewSet):
    filter_fields = ('name', 'level1_uuid')
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    queryset = WorkflowLevel1.objects.all()
    serializer_class = WorkflowLevel1Serializer


class WorkflowLevel2ViewSet(viewsets.ModelViewSet):
    filter_fields = ('name', 'workflowlevel1__name', 'level2_uuid')
    filter_backends = (django_filters.rest_framework.DjangoFilterBackend,)
    queryset = WorkflowLevel2.objects.all()
    serializer_class = WorkflowLevel2Serializer


class PublicSiloViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PublicSiloSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,
                          IsOwnerOrReadOnly,)
    lookup_field = 'id'

    def get_queryset(self):
        return Silo.objects.filter(public=True)

    @detail_route()
    def data(self, request, id):
        if id <= 0:
            return HttpResponseBadRequest("The silo_id = %s is invalid" % id)

        silo = Silo.objects.get(pk=id)
        if silo.public == False:
            return HttpResponse("This table is not public. You must use the private API.")
        query = request.GET.get('query',"{}")
        filter_fields = json.loads(query)

        shown_cols = set(json.loads(request.GET.get('shown_cols',json.dumps(getSiloColumnNames(id)))))


        recordsTotal = LabelValueStore.objects(silo_id=id, **filter_fields).count()


        #print("offset=%s length=%s" % (offset, length))
        #page_size = 100
        #page = int(request.GET.get('page', 1))
        #offset = (page - 1) * page_size
        #if page > 0:
        # workaround until the problem of javascript not increasing the value of length is fixed
        data = LabelValueStore.objects(silo_id=id, **filter_fields).exclude('create_date', 'edit_date', 'silo_id','read_id')

        for col in getCompleteSiloColumnNames(id):
            if col not in shown_cols:
                data = data.exclude(col)

        sort = str(request.GET.get('sort',''))
        data = data.order_by(sort)
        json_data = json.loads(data.to_json())
        return JsonResponse(json_data, safe=False)


class CustomFormViewSet(mixins.CreateModelMixin,
                        mixins.UpdateModelMixin,
                        viewsets.GenericViewSet):
    serializer_class = CustomFormSerializer
    queryset = Silo.objects.all()

    def create(self, request, *args, **kwargs):
        """
        Create a table for the form instance in Activity
        """
        if not request.user.is_superuser:
            return Response(status=status.HTTP_403_FORBIDDEN)

        try:
            form_uuid = request.POST['form_uuid']
            level1_uuid = request.POST['level1_uuid']
            tola_user_uuid = request.POST['tola_user_uuid']
            wkflvl1 = WorkflowLevel1.objects.get(level1_uuid=level1_uuid)
            tola_user = TolaUser.objects.get(tola_user_uuid=tola_user_uuid)
            form_name = request.POST['name']
            read_name = request.POST['name']
            columns = request.POST['fields']
        except (WorkflowLevel1.DoesNotExist, TolaUser.DoesNotExist, KeyError) \
                as e:
            return Response(e, status=status.HTTP_400_BAD_REQUEST)

        url_subpath = '/activity/forms/{}/view'.format(form_uuid)
        form_url = urljoin(settings.ACTIVITY_URL, url_subpath)
        read = Read.objects.create(
            owner=tola_user.user,
            type=ReadType.objects.get(read_type='CustomForm'),
            read_name=read_name,
            read_url=form_url
        )

        table_name = '{} - {}'.format(form_name, wkflvl1.name)
        if len(table_name) > 255:
            table_name = table_name[:255]
        description = request.POST.get('description', '')
        silo = Silo.objects.create(
            owner=tola_user.user,
            name=table_name,
            description=description,
            organization=tola_user.organization,
            public=False,
            columns=columns,
            form_uuid=form_uuid
        )

        silo.reads.add(read)
        silo.workflowlevel1.add(wkflvl1)

        serializer = self.serializer_class(silo, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """
        Update only some specific info from silo
        """
        if not request.user.is_superuser:
            return Response(status=status.HTTP_403_FORBIDDEN)

        silo = self.get_object()
        wkflvl1 = silo.workflowlevel1.first()
        read = silo.reads.first()

        try:
            table_name = request.data['name'].lower().replace(' ', '_')
            table_name += '_' + wkflvl1.name.lower().replace(' ', '_')
            read_name = request.data['name']
            columns = request.data['fields']
        except KeyError as e:
            return Response(e, status=status.HTTP_400_BAD_REQUEST)
        description = request.data.get('description', '')

        read.read_name = read_name
        read.save()

        silo.name = table_name
        silo.description = description
        silo.columns = columns
        silo.save()

        serializer = self.get_serializer(silo)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @detail_route(methods=['GET'],)
    def has_data(self, request, pk):
        """
        Check if the data was added to the custom form instance
        """
        if not request.user.is_superuser:
            return Response(status=status.HTTP_403_FORBIDDEN)
        silo = self.get_object()
        return Response(silo.data_count > 1, status=status.HTTP_200_OK)

    @list_route(methods=['POST'], permission_classes=[AllowAny])
    def save_data(self, request):
        """
        Persist user input data
        """
        if not request.data:
            return Response({'detail': 'No data sent.'},
                            status=status.HTTP_400_BAD_REQUEST)

        if 'silo_id' in request.data and 'data' in request.data:
            silo_id = request.data['silo_id']
            data = request.data['data']
        else:
            return Response({'detail': 'Missing data.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            silo = Silo.objects.get(pk=silo_id)
        except Silo.DoesNotExist:
            return Response({'detail': 'Not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        else:
            save_data_to_silo(silo, [data], silo.reads.first())
            return Response({'detail': 'It was successfully saved.'},
                            status=status.HTTP_200_OK)


class SilosByUser(viewsets.ReadOnlyModelViewSet):
    """
    Lists all silos by a user; returns data in a format
    understood by Ember DataStore.
    """
    serializer_class = SiloSerializer
    parser_classes = (JSONParser,)
    renderer_classes = (JSONRenderer,)

    def get_queryset(self):
        silos = Silo.objects.all()
        user_id = self.request.query_params.get("user_id", None)
        if user_id:
            silos = silos.filter(owner__id=user_id)
        return silos


class SiloViewSet(viewsets.ReadOnlyModelViewSet):
    """
    This viewset automatically provides `list` and `retrieve` actions.
    """
    serializer_class = SiloSerializer
    lookup_field = 'id'
    # this permission sets seems to break the default permissions set by the restframework
    # permission_classes = (IsOwnerOrReadOnly,)
    permission_classes = (IsAuthenticated, Silo_IsOwnerOrCanRead,)
    filter_fields = ('owner__username','shared__username','id','tags','public')
    filter_backends = (filters.DjangoFilterBackend,)

    def get_queryset(self):
        user_uuid = self.request.GET.get('user_uuid')
        if user_uuid is not None:
            try:
                tola_user = TolaUser.objects.get(tola_user_uuid=user_uuid)
            except TolaUser.DoesNotExist:
                return Silo.objects.filter(owner=None)
            else:
                user = tola_user.user
                return Silo.objects.filter(
                    Q(owner=user) | Q(public=True) | Q(shared=user))
        else:
            user = self.request.user
            if user.is_superuser:
                return Silo.objects.all()

            return Silo.objects.filter(Q(owner=user) | Q(public=True))

    @detail_route()
    def data(self, request, id):
        # calling get_object applies the permission classes to this query
        self.get_object()

        # get the mongo collection
        collection = LabelValueStore._get_collection()

        draw = int(request.GET.get("draw", 1))
        offset = int(request.GET.get('start', -1))
        length = int(request.GET.get('length', 10))

        # filtering syntax is the mongodb syntax
        query = request.GET.get('query', '{}')
        group = request.GET.get('group', '{}')
        sort = str(request.GET.get('sort', '{}'))
        query_fields = json.loads(query)
        group_fields = json.loads(group)
        sort_fields = json.loads(sort)

        # creating the aggregation pipeline
        pipeline = [
            {'$match': {'$and': [{'silo_id': int(id)}, query_fields]}},
            {'$project': {
                'create_date': 0,
                'edit_date': 0,
                'silo_id': 0,
                'read_id': 0
            }}
        ]
        if group_fields:
            pipeline.append({'$group': group_fields})
        if sort_fields:
            pipeline.append({'$sort': sort_fields})

        count_pipeline = pipeline + [{'$count': 'count'}]
        count_cur = collection.aggregate(pipeline=count_pipeline)
        list_count = list(count_cur)
        if list_count:
            count_result = list_count[0]
            records_total = count_result['count']
        else:
            records_total = 0

        # workaround until the problem of javascript not increasing
        # the value of length is fixed
        if offset >= 0:
            length = offset + length
            pipeline.append({'$skip': offset})
            pipeline.append({'$limit': length})

        pipeline_cur = collection.aggregate(pipeline=pipeline)
        pipeline_result = list(pipeline_cur)
        data = JSONEncoder().encode(pipeline_result)
        json_data = json.loads(data)

        return JsonResponse({"data": json_data, "draw": draw,
                             "recordsTotal": records_total,
                             "recordsFiltered": records_total},  safe=False)


class TagViewSet(viewsets.ModelViewSet):
    """
    This viewset automatically provides `list`, `create`, `retrieve`,
    `update` and `destroy` actions.
    """
    queryset = Tag.objects.all()
    serializer_class = TagSerializer


class ReadViewSet(viewsets.ModelViewSet):
    """
    This viewset automatically provides `list` and `retrieve`, actions.
    """
    serializer_class = ReadSerializer
    permission_classes = (IsAuthenticated, ReadIsOwnerViewOrWrite)

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return Read.objects.all()
        return Read.objects.filter(Q(owner=user) | Q(silos__public=True) |
                                   Q(silos__shared=self.request.user))


class ReadTypeViewSet(viewsets.ModelViewSet):
    """
    This viewset automatically provides `list`, `create`, `retrieve`,
    `update` and `destroy` actions.
    """
    queryset = ReadType.objects.all()
    serializer_class = ReadTypeSerializer


# ####-------API Views to Feed Data to Tolawork API requests-----### #
'''
    This view responds to the 'GET' request from TolaWork
'''


@api_view(['GET'])
@authentication_classes(())
@permission_classes(())
def tables_api_view(request):
    """
    Get TolaTables Tables owned by a user logged in Tolawork & a list of
    logged in Users,
    """
    if request.method == 'GET':
        user = request.GET.get('email')

        user_id = User.objects.get(email=user).id

        tables = Silo.objects.filter(owner=user_id).order_by('-create_date')
        table_logged_users = logged_in_users()

        table_serializer = SiloModelSerializer(tables, many=True)
        user_serializer = LoggedUserSerializer(table_logged_users, many=True)

        users = user_serializer.data
        tables = table_serializer.data
        tables_data = {'tables':tables, 'table_logged_users': users}

        return Response(tables_data)


# return users logged into TolaActivity
def logged_in_users():
    logged_users = LoggedUser.objects.order_by('username')
    for logged_user in logged_users:
        logged_user.queue = 'TolaTables'

    return logged_users
