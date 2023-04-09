from asgiref.sync import async_to_sync
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.http import JsonResponse
from rest_framework.authtoken.models import Token
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.db.models import F, Sum, Q
from ujson import loads as json
from django.db import IntegrityError

from .mail import new_order
from .tasks import get_import, send_email

from rest_framework.generics import RetrieveAPIView, ListAPIView
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle, AnonRateThrottle
from rest_framework.views import APIView

from orders.models import ProductInfo, Shop, Order, ConfirmEmailToken, OrderItem
from orders.serializers import UserSerializer, ProductSerializer, ShopSerializer, OrderSerializer, \
    ProductInfoSerializer, OrderItemSerializer


class RegisterAccountView(APIView):
    # регистрация
    def post(self, request, *args, **kwargs):
        if {'first_name', 'last_name', 'email', 'password', 'company', 'position'}.issuperset(request.data):
            try:
                validate_password(request.data['password'])
            except Exception as password_error:
                error_array = []
                for item in password_error:
                    error_array.append(item)
                return JsonResponse({'Status': False, 'Errors': {'password': error_array}})
            else:
                # проверка данных уникальности имени
                request.data._mutable = True
                request.data.update({})
                user_serializer = UserSerializer(data=request.data)
                if user_serializer.is_valid():
                    # сохраняем пользователя
                    user = user_serializer.save()
                    user.set_password(request.data['password'])
                    user.save()
                    token, _ = ConfirmEmailToken.objects.get_or_create(user_id=user.id)
                    send_email.delay('Confirmation of registration', f'Confirmation token: {token.key}',
                                         user.email)
                    return JsonResponse({'Status': True, 'Token for email confirmation': token.key})
                else:
                    return JsonResponse({'Status': False, 'Errors': user_serializer.errors})

        return JsonResponse({'Status': False, 'Errors': 'All required arguments not provided'})


class LoginView(APIView):
    # авторизация пользователя
    def post(self, request, *args, **kwargs):
        if {'email', 'password'}.issubset(request.data):
            user = authenticate(request, username=request.data['email'], password=request.data['password'])
            if user is not None:
                if user.is_active:
                    token, created = Token.objects.get_or_create(user=user)

                    return JsonResponse({'Status': True, 'token': token.key})

            else:
                return JsonResponse({'Status': False, 'error': 'Invalid username or password'})


        return JsonResponse({'Status': False, 'Errors': 'All required arguments not provided'})


class ProductsView(APIView):
    # просмотр всего каталога товаров
    def get(self, request):
        products = ProductInfo.objects.all()
        ser = ProductInfoSerializer(products, many=True)
        return Response(ser.data)

class ProductView(RetrieveAPIView):
    # просмотр определенного товара
    queryset = ProductInfo.objects.all()
    serializer_class = ProductInfoSerializer

class ProductsByShop(ListAPIView):
    # просмотр товаров определенного магазина
    serializer_class = ProductInfoSerializer
    def get_queryset(self):
        shop_id = self.kwargs['shop_id']
        return ProductInfo.objects.filter(shop_id=shop_id).select_related('product')

class ShopView(ListAPIView):
    # просмотр списка магазинов
    queryset = Shop.objects.filter(state=True)
    serializer_class = ShopSerializer

class StatusOrderView(RetrieveAPIView):
    # получение статуса заказа
    queryset = Order.objects.all()
    serializer_class = OrderSerializer



class BasketView(APIView):
    throttle_classes = (UserRateThrottle,)
    # получить корзину
    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'})
        basket = Order.objects.filter(
            user_id=request.user.id, state='basket').prefetch_related(
            'ordered_items__product_info__product__category',
            'ordered_items__product_info__product_parameters__parameter').annotate(
            total_sum=Sum(F('ordered_items__quantity') * F('ordered_items__product_info__price'))).distinct()

        serializer = OrderSerializer(basket, many=True)
        return Response(serializer.data)

    # редактировать корзину
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'})

        items_sting = request.data.get('items')
        if items_sting:
            try:
                items_dict = json(items_sting)
            except ValueError:
                JsonResponse({'Status': False, 'Errors': 'Неверный формат запроса'})
            else:
                basket, _ = Order.objects.get_or_create(user_id=request.user.id, state='basket')
                objects_created = 0
                for order_item in items_dict:
                    order_item.update({'order': basket.id})
                    serializer = OrderItemSerializer(data=order_item)
                    if serializer.is_valid():
                        try:
                            serializer.save()
                        except IntegrityError as error:
                            return JsonResponse({'Status': False, 'Errors': str(error)})
                        else:
                            objects_created += 1

                    else:

                        JsonResponse({'Status': False, 'Errors': serializer.errors})

                return JsonResponse({'Status': True, 'Создано объектов': objects_created})
        return JsonResponse({'Status': False, 'Errors': 'Не указаны все необходимые аргументы'})

    # удалить товары из корзины
    def delete(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'}, status=403)

        items_sting = request.data.get('items')
        if items_sting:
            items_list = items_sting.split(',')
            basket, _ = Order.objects.get_or_create(user_id=request.user.id, state='basket')
            query = Q()
            objects_deleted = False
            for order_item_id in items_list:
                if order_item_id.isdigit():
                    query = query | Q(order_id=basket.id, id=order_item_id)
                    objects_deleted = True

            if objects_deleted:
                deleted_count = OrderItem.objects.filter(query).delete()[0]
                return JsonResponse({'Status': True, 'Удалено объектов': deleted_count})
        return JsonResponse({'Status': False, 'Errors': 'Не указаны все необходимые аргументы'})

    # добавить позиции в корзину
    def put(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'}, status=403)

        items_sting = request.data.get('items')
        if items_sting:
            try:
                items_dict = json(items_sting)
            except ValueError:
                JsonResponse({'Status': False, 'Errors': 'Неверный формат запроса'})
            else:
                basket, _ = Order.objects.get_or_create(user_id=request.user.id, state='basket')
                objects_updated = 0
                for order_item in items_dict:
                    if type(order_item['id']) == int and type(order_item['quantity']) == int:
                        objects_updated += OrderItem.objects.filter(order_id=basket.id, id=order_item['id']).update(
                            quantity=order_item['quantity'])

                return JsonResponse({'Status': True, 'Обновлено объектов': objects_updated})
        return JsonResponse({'Status': False, 'Errors': 'Не указаны все необходимые аргументы'})

class UpdatePriceView(APIView):
    throttle_classes = (UserRateThrottle,)
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'})

        if request.user.type != 'shop':
            return JsonResponse({'Status': False, 'Error': 'For shops only'})

        url = request.data.get('url')
        if url:
            try:
                task = get_import.delay(url, request.user.id)
            except IntegrityError as e:
                return JsonResponse({'Status': False,
                                     'Errors': f'Integrity Error: {e}'})

            return JsonResponse({'Status': True})

        return JsonResponse({'Status': False, 'Errors': 'All necessary arguments are not specified'})

class MyOrderView(APIView):

    throttle_classes = (UserRateThrottle,)
    # получить мои заказы
    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'})
        order = Order.objects.filter(
            user_id=request.user.id).exclude(state='basket').prefetch_related(
            'ordered_items__product_info__product__category',
            'ordered_items__product_info__product_parameters__parameter').select_related('contact').annotate(
            total_sum=Sum(F('ordered_items__quantity') * F('ordered_items__product_info__price'))).distinct()

        serializer = OrderSerializer(order, many=True)
        return Response(serializer.data)

    # разместить заказ из корзины
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'Status': False, 'Error': 'Log in required'})

        if {'id', 'contact'}.issubset(request.data):
            if request.data['id'].isdigit():
                try:
                    is_updated = Order.objects.filter(
                        user_id=request.user.id, id=request.data['id']).update(
                        contact_id=request.data['contact'],
                        state='new')
                except IntegrityError as error:
                    print(error)
                    return JsonResponse({'Status': False, 'Errors': 'Неправильно указаны аргументы'})
                else:
                    if is_updated:
                        new_order.send(sender=self.__class__, user_id=request.user.id)
                        return JsonResponse({'Status': True})

        return JsonResponse({'Status': False, 'Errors': 'Не указаны все необходимые аргументы'})
