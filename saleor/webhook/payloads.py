import json
import uuid
from collections import defaultdict
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, DefaultDict, Dict, Iterable, List, Optional, Set

import graphene
from django.contrib.auth.models import AnonymousUser
from django.db.models import F, QuerySet, Sum
from django.utils import timezone
from graphene.utils.str_converters import to_camel_case

from .. import __version__
from ..account.models import User
from ..attribute.models import AttributeValueTranslation
from ..checkout.models import Checkout
from ..core.prices import quantize_price, quantize_price_fields
from ..core.utils import build_absolute_uri
from ..core.utils.anonymization import (
    anonymize_checkout,
    anonymize_order,
    generate_fake_user,
)
from ..core.utils.json_serializer import CustomJsonEncoder
from ..discount.utils import fetch_active_discounts
from ..order import FulfillmentStatus, OrderStatus
from ..order.models import Fulfillment, FulfillmentLine, Order, OrderLine
from ..order.utils import get_order_country
from ..page.models import Page
from ..payment import ChargeStatus
from ..plugins.webhook.utils import from_payment_app_id
from ..product import ProductMediaTypes
from ..product.models import Collection, Product
from ..shipping.interface import ShippingMethodData
from ..warehouse.models import Stock, Warehouse
from . import traced_payload_generator
from .event_types import WebhookEventAsyncType
from .payload_serializers import PayloadSerializer
from .serializers import (
    serialize_checkout_lines,
    serialize_product_or_variant_attributes,
)

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from ..product.models import ProductVariant


if TYPE_CHECKING:
    from ..discount.models import Sale
    from ..invoice.models import Invoice
    from ..payment.interface import PaymentData, TransactionActionData
    from ..payment.models import Payment
    from ..plugins.base_plugin import RequestorOrLazyObject
    from ..translation.models import Translation


ADDRESS_FIELDS = (
    "first_name",
    "last_name",
    "company_name",
    "street_address_1",
    "street_address_2",
    "city",
    "city_area",
    "postal_code",
    "country",
    "country_area",
    "phone",
)

ORDER_FIELDS = (
    "status",
    "origin",
    "shipping_method_name",
    "collection_point_name",
    "shipping_price_net_amount",
    "shipping_price_gross_amount",
    "shipping_tax_rate",
    "weight",
    "language_code",
    "private_metadata",
    "metadata",
    "total_net_amount",
    "total_gross_amount",
    "undiscounted_total_net_amount",
    "undiscounted_total_gross_amount",
)

ORDER_PRICE_FIELDS = (
    "shipping_price_net_amount",
    "shipping_price_gross_amount",
    "total_net_amount",
    "total_gross_amount",
    "undiscounted_total_net_amount",
    "undiscounted_total_gross_amount",
)


def generate_requestor(requestor: Optional["RequestorOrLazyObject"] = None):
    if not requestor:
        return {"id": None, "type": None}
    if isinstance(requestor, (User, AnonymousUser)):
        return {"id": graphene.Node.to_global_id("User", requestor.id), "type": "user"}
    return {"id": requestor.name, "type": "app"}  # type: ignore


def generate_meta(*, requestor_data: Dict[str, Any], camel_case=False, **kwargs):
    meta_result = {
        "issued_at": timezone.now().isoformat(),
        "version": __version__,
        "issuing_principal": requestor_data,
    }

    meta_result.update(kwargs)

    if camel_case:
        meta = {}
        for key, value in meta_result.items():
            meta[to_camel_case(key)] = value
    else:
        meta = meta_result

    return meta


def prepare_order_lines_allocations_payload(line):
    warehouse_id_quantity_allocated_map = list(
        line.allocations.values(  # type: ignore
            "quantity_allocated",
            warehouse_id=F("stock__warehouse_id"),
        )
    )
    for item in warehouse_id_quantity_allocated_map:
        item["warehouse_id"] = graphene.Node.to_global_id(
            "Warehouse", item["warehouse_id"]
        )
    return warehouse_id_quantity_allocated_map


@traced_payload_generator
def generate_order_lines_payload(lines: Iterable[OrderLine]):
    line_fields = (
        "product_name",
        "variant_name",
        "translated_product_name",
        "translated_variant_name",
        "product_sku",
        "quantity",
        "currency",
        "unit_price_net_amount",
        "unit_price_gross_amount",
        "unit_discount_amount",
        "unit_discount_type",
        "unit_discount_reason",
        "total_price_net_amount",
        "total_price_gross_amount",
        "undiscounted_unit_price_net_amount",
        "undiscounted_unit_price_gross_amount",
        "undiscounted_total_price_net_amount",
        "undiscounted_total_price_gross_amount",
        "tax_rate",
        "sale_id",
        "voucher_code",
    )
    line_price_fields = (
        "unit_price_gross_amount",
        "unit_price_net_amount",
        "unit_discount_amount",
        "total_price_net_amount",
        "total_price_gross_amount",
        "undiscounted_unit_price_net_amount",
        "undiscounted_unit_price_gross_amount",
        "undiscounted_total_price_net_amount",
        "undiscounted_total_price_gross_amount",
    )

    for line in lines:
        quantize_price_fields(line, line_price_fields, line.currency)

    serializer = PayloadSerializer()
    return serializer.serialize(
        lines,
        fields=line_fields,
        extra_dict_data={
            "product_variant_id": (lambda l: l.product_variant_id),
            "total_price_net_amount": (lambda l: l.total_price.net.amount),
            "total_price_gross_amount": (lambda l: l.total_price.gross.amount),
            "allocations": (lambda l: prepare_order_lines_allocations_payload(l)),
        },
    )


def _generate_collection_point_payload(warehouse: "Warehouse"):
    serializer = PayloadSerializer()
    collection_point_fields = (
        "name",
        "email",
        "click_and_collect_option",
        "is_private",
    )
    collection_point_data = serializer.serialize(
        [warehouse],
        fields=collection_point_fields,
        additional_fields={"address": (lambda w: w.address, ADDRESS_FIELDS)},
    )
    return collection_point_data


def _generate_shipping_method_payload(shipping_method, channel):
    if not shipping_method:
        return None

    serializer = PayloadSerializer()
    shipping_method_fields = ("name", "type")

    shipping_method_channel_listing = shipping_method.channel_listings.filter(
        channel=channel,
    ).first()

    payload = serializer.serialize(
        [shipping_method],
        fields=shipping_method_fields,
        extra_dict_data={
            "currency": shipping_method_channel_listing.currency,
            "price_amount": quantize_price(
                shipping_method_channel_listing.price_amount,
                shipping_method_channel_listing.currency,
            ),
        },
    )

    return json.loads(payload)[0]


@traced_payload_generator
def generate_order_payload(
    order: "Order",
    requestor: Optional["RequestorOrLazyObject"] = None,
    with_meta: bool = True,
):
    serializer = PayloadSerializer()
    fulfillment_fields = (
        "status",
        "tracking_number",
        "shipping_refund_amount",
        "total_refund_amount",
    )
    fulfillment_price_fields = ("shipping_refund_amount", "total_refund_amount")
    payment_price_fields = ("captured_amount", "total")
    discount_fields = (
        "type",
        "value_type",
        "value",
        "amount_value",
        "name",
        "translated_name",
        "reason",
    )
    discount_price_fields = ("amount_value",)

    channel_fields = ("slug", "currency_code")

    lines = order.lines.all()
    fulfillments = order.fulfillments.all()
    payments = order.payments.all()
    discounts = order.discounts.all()

    quantize_price_fields(order, ORDER_PRICE_FIELDS, order.currency)

    for fulfillment in fulfillments:
        quantize_price_fields(fulfillment, fulfillment_price_fields, order.currency)

    for payment in payments:
        quantize_price_fields(payment, payment_price_fields, order.currency)

    for discount in discounts:
        quantize_price_fields(discount, discount_price_fields, order.currency)

    fulfillments_data = serializer.serialize(
        fulfillments,
        fields=fulfillment_fields,
        extra_dict_data={
            "lines": lambda f: json.loads(generate_fulfillment_lines_payload(f)),
            "created": lambda f: f.created_at,
        },
    )

    extra_dict_data = {
        "id": graphene.Node.to_global_id("Order", order.id),
        "token": str(order.id),
        "user_email": order.get_customer_email(),
        "created": order.created_at,
        "original": graphene.Node.to_global_id("Order", order.original_id),
        "lines": json.loads(generate_order_lines_payload(lines)),
        "fulfillments": json.loads(fulfillments_data),
        "collection_point": json.loads(
            _generate_collection_point_payload(order.collection_point)
        )[0]
        if order.collection_point
        else None,
        "payments": json.loads(_generate_order_payment_payload(payments)),
        "shipping_method": _generate_shipping_method_payload(
            order.shipping_method, order.channel
        ),
    }
    if with_meta:
        extra_dict_data["meta"] = generate_meta(
            requestor_data=generate_requestor(requestor)
        )

    order_data = serializer.serialize(
        [order],
        fields=ORDER_FIELDS,
        additional_fields={
            "channel": (lambda o: o.channel, channel_fields),
            "shipping_address": (lambda o: o.shipping_address, ADDRESS_FIELDS),
            "billing_address": (lambda o: o.billing_address, ADDRESS_FIELDS),
            "discounts": (lambda _: discounts, discount_fields),
        },
        extra_dict_data=extra_dict_data,
    )
    return order_data


def _generate_order_payment_payload(payments: Iterable["Payment"]):
    payment_fields = (
        "gateway",
        "payment_method_type",
        "cc_brand",
        "is_active",
        "partial",
        "charge_status",
        "psp_reference",
        "total",
        "captured_amount",
        "currency",
        "billing_email",
        "billing_first_name",
        "billing_last_name",
        "billing_company_name",
        "billing_address_1",
        "billing_address_2",
        "billing_city",
        "billing_city_area",
        "billing_postal_code",
        "billing_country_code",
        "billing_country_area",
    )
    serializer = PayloadSerializer()
    return serializer.serialize(
        payments,
        fields=payment_fields,
        extra_dict_data={
            "created": lambda p: p.created_at,
            "modified": lambda p: p.modified_at,
        },
    )


def _calculate_added(
    previous_catalogue: DefaultDict[str, Set[str]],
    current_catalogue: DefaultDict[str, Set[str]],
    key: str,
) -> List[str]:
    return list(current_catalogue[key] - previous_catalogue[key])


def _calculate_removed(
    previous_catalogue: DefaultDict[str, Set[str]],
    current_catalogue: DefaultDict[str, Set[str]],
    key: str,
) -> List[str]:
    return _calculate_added(current_catalogue, previous_catalogue, key)


@traced_payload_generator
def generate_sale_payload(
    sale: "Sale",
    previous_catalogue: Optional[DefaultDict[str, Set[str]]] = None,
    current_catalogue: Optional[DefaultDict[str, Set[str]]] = None,
    requestor: Optional["RequestorOrLazyObject"] = None,
):
    if previous_catalogue is None:
        previous_catalogue = defaultdict(set)
    if current_catalogue is None:
        current_catalogue = defaultdict(set)

    serializer = PayloadSerializer()
    sale_fields = ("id",)

    return serializer.serialize(
        [sale],
        fields=sale_fields,
        extra_dict_data={
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
            "categories_added": _calculate_added(
                previous_catalogue, current_catalogue, "categories"
            ),
            "categories_removed": _calculate_removed(
                previous_catalogue, current_catalogue, "categories"
            ),
            "collections_added": _calculate_added(
                previous_catalogue, current_catalogue, "collections"
            ),
            "collections_removed": _calculate_removed(
                previous_catalogue, current_catalogue, "collections"
            ),
            "products_added": _calculate_added(
                previous_catalogue, current_catalogue, "products"
            ),
            "products_removed": _calculate_removed(
                previous_catalogue, current_catalogue, "products"
            ),
            "variants_added": _calculate_added(
                previous_catalogue, current_catalogue, "variants"
            ),
            "variants_removed": _calculate_removed(
                previous_catalogue, current_catalogue, "variants"
            ),
        },
    )


@traced_payload_generator
def generate_invoice_payload(
    invoice: "Invoice", requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    invoice_fields = ("id", "number", "external_url", "created")
    if invoice.order is not None:
        quantize_price_fields(invoice.order, ORDER_PRICE_FIELDS, invoice.order.currency)
    return serializer.serialize(
        [invoice],
        fields=invoice_fields,
        extra_dict_data={
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
            "order": lambda i: json.loads(_generate_order_payload_for_invoice(i.order))[
                0
            ],
        },
    )


@traced_payload_generator
def _generate_order_payload_for_invoice(order: "Order"):
    # This is a temporary method that allows attaching an order token
    # that is no longer part of the order model.
    # The method should be removed after removing the deprecated order token field.
    # After that, we should move generating order data to the `additional_fields`
    # in the `generate_invoice_payload` method.
    serializer = PayloadSerializer()
    payload = serializer.serialize(
        [order],
        fields=ORDER_FIELDS,
        extra_dict_data={
            "token": order.id,
            "user_email": order.get_customer_email(),
            "created": order.created_at,
        },
    )
    return payload


@traced_payload_generator
def generate_checkout_payload(
    checkout: "Checkout", requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    checkout_fields = (
        "last_change",
        "status",
        "email",
        "quantity",
        "currency",
        "discount_amount",
        "discount_name",
        "language_code",
        "private_metadata",
        "metadata",
    )

    checkout_price_fields = ("discount_amount",)
    quantize_price_fields(checkout, checkout_price_fields, checkout.currency)
    user_fields = ("email", "first_name", "last_name")
    channel_fields = ("slug", "currency_code")

    discounts = fetch_active_discounts()
    lines_dict_data = serialize_checkout_lines(checkout, discounts)

    # todo use the most appropriate warehouse
    warehouse = None
    if checkout.shipping_address:
        warehouse = Warehouse.objects.for_country(
            checkout.shipping_address.country.code
        ).first()

    checkout_data = serializer.serialize(
        [checkout],
        fields=checkout_fields,
        obj_id_name="token",
        additional_fields={
            "channel": (lambda o: o.channel, channel_fields),
            "user": (lambda c: c.user, user_fields),
            "billing_address": (lambda c: c.billing_address, ADDRESS_FIELDS),
            "shipping_address": (lambda c: c.shipping_address, ADDRESS_FIELDS),
            "warehouse_address": (
                lambda c: warehouse.address if warehouse else None,
                ADDRESS_FIELDS,
            ),
        },
        extra_dict_data={
            # Casting to list to make it json-serializable
            "shipping_method": _generate_shipping_method_payload(
                checkout.shipping_method, checkout.channel
            ),
            "lines": list(lines_dict_data),
            "collection_point": json.loads(
                _generate_collection_point_payload(checkout.collection_point)
            )[0]
            if checkout.collection_point
            else None,
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
            "created": checkout.created_at,
        },
    )
    return checkout_data


@traced_payload_generator
def generate_customer_payload(
    customer: "User", requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    data = serializer.serialize(
        [customer],
        fields=[
            "email",
            "first_name",
            "last_name",
            "is_active",
            "date_joined",
            "language_code",
            "private_metadata",
            "metadata",
        ],
        additional_fields={
            "default_shipping_address": (
                lambda c: c.default_shipping_address,
                ADDRESS_FIELDS,
            ),
            "default_billing_address": (
                lambda c: c.default_billing_address,
                ADDRESS_FIELDS,
            ),
            "addresses": (
                lambda c: c.addresses.all(),
                ADDRESS_FIELDS,
            ),
        },
        extra_dict_data={
            "meta": generate_meta(requestor_data=generate_requestor(requestor))
        },
    )
    return data


@traced_payload_generator
def generate_collection_payload(
    collection: "Collection", requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    data = serializer.serialize(
        [collection],
        fields=[
            "name",
            "description",
            "background_image_alt",
            "private_metadata",
            "metadata",
        ],
        extra_dict_data={
            "background_image": build_absolute_uri(collection.background_image.url)
            if collection.background_image
            else None,
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
        },
    )
    return data


PRODUCT_FIELDS = (
    "name",
    "description",
    "currency",
    "updated_at",
    "charge_taxes",
    "weight",
    "publication_date",
    "is_published",
    "private_metadata",
    "metadata",
)


def serialize_product_channel_listing_payload(channel_listings):
    serializer = PayloadSerializer()
    fields = (
        "published_at",
        "is_published",
        "visible_in_listings",
        "available_for_purchase_at",
    )
    channel_listing_payload = serializer.serialize(
        channel_listings,
        fields=fields,
        extra_dict_data={
            "channel_slug": lambda pch: pch.channel.slug,
            # deprecated in 3.3 - published_at and available_for_purchase_at
            # should be used instead
            "publication_date": lambda pch: pch.published_at,
            "available_for_purchase": lambda pch: pch.available_for_purchase_at,
        },
    )
    return channel_listing_payload


@traced_payload_generator
def generate_product_payload(
    product: "Product", requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer(
        extra_model_fields={"ProductVariant": ("quantity", "quantity_allocated")}
    )
    product_payload = serializer.serialize(
        [product],
        fields=PRODUCT_FIELDS,
        additional_fields={
            "category": (lambda p: p.category, ("name", "slug")),
            "collections": (lambda p: p.collections.all(), ("name", "slug")),
        },
        extra_dict_data={
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
            "attributes": serialize_product_or_variant_attributes(product),
            "media": [
                {
                    "alt": media_obj.alt,
                    "url": (
                        build_absolute_uri(media_obj.image.url)
                        if media_obj.type == ProductMediaTypes.IMAGE
                        else media_obj.external_url
                    ),
                }
                for media_obj in product.media.all()
            ],
            "channel_listings": json.loads(
                serialize_product_channel_listing_payload(
                    product.channel_listings.all()  # type: ignore
                )
            ),
            "variants": lambda x: json.loads(
                (generate_product_variant_payload(x, with_meta=False))
            ),
        },
    )
    return product_payload


@traced_payload_generator
def generate_product_deleted_payload(
    product: "Product", variants_id, requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    product_fields = PRODUCT_FIELDS
    product_variant_ids = [
        graphene.Node.to_global_id("ProductVariant", pk) for pk in variants_id
    ]
    product_payload = serializer.serialize(
        [product],
        fields=product_fields,
        extra_dict_data={
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
            "variants": list(product_variant_ids),
        },
    )
    return product_payload


PRODUCT_VARIANT_FIELDS = (
    "sku",
    "name",
    "track_inventory",
    "private_metadata",
    "metadata",
)


@traced_payload_generator
def generate_product_variant_listings_payload(variant_channel_listings):
    serializer = PayloadSerializer()
    fields = (
        "currency",
        "price_amount",
        "cost_price_amount",
    )
    channel_listing_payload = serializer.serialize(
        variant_channel_listings,
        fields=fields,
        extra_dict_data={"channel_slug": lambda vch: vch.channel.slug},
    )
    return channel_listing_payload


@traced_payload_generator
def generate_product_variant_media_payload(product_variant):
    return [
        {
            "alt": media_obj.media.alt,
            "url": (
                build_absolute_uri(media_obj.media.image.url)
                if media_obj.media.type == ProductMediaTypes.IMAGE
                else media_obj.media.external_url
            ),
        }
        for media_obj in product_variant.variant_media.all()
    ]


@traced_payload_generator
def generate_product_variant_with_stock_payload(
    stocks: Iterable["Stock"], requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    extra_dict_data = {
        "product_id": lambda v: graphene.Node.to_global_id(
            "Product", v.product_variant.product_id
        ),
        "product_variant_id": lambda v: graphene.Node.to_global_id(
            "ProductVariant", v.product_variant_id
        ),
        "warehouse_id": lambda v: graphene.Node.to_global_id(
            "Warehouse", v.warehouse_id
        ),
        "product_slug": lambda v: v.product_variant.product.slug,
        "meta": generate_meta(requestor_data=generate_requestor(requestor)),
    }
    return serializer.serialize(stocks, fields=[], extra_dict_data=extra_dict_data)


@traced_payload_generator
def generate_product_variant_payload(
    product_variants: Iterable["ProductVariant"],
    requestor: Optional["RequestorOrLazyObject"] = None,
    with_meta: bool = True,
):
    extra_dict_data = {
        "id": lambda v: v.get_global_id(),
        "attributes": lambda v: serialize_product_or_variant_attributes(v),
        "product_id": lambda v: graphene.Node.to_global_id("Product", v.product_id),
        "media": lambda v: generate_product_variant_media_payload(v),
        "channel_listings": lambda v: json.loads(
            generate_product_variant_listings_payload(v.channel_listings.all())
        ),
    }

    if with_meta:
        extra_dict_data["meta"] = generate_meta(
            requestor_data=generate_requestor(requestor)
        )

    serializer = PayloadSerializer()
    payload = serializer.serialize(
        product_variants,
        fields=PRODUCT_VARIANT_FIELDS,
        extra_dict_data=extra_dict_data,
    )
    return payload


@traced_payload_generator
def generate_product_variant_stocks_payload(product_variant: "ProductVariant"):
    return product_variant.stocks.aggregate(Sum("quantity"))["quantity__sum"] or 0


@traced_payload_generator
def generate_fulfillment_lines_payload(fulfillment: Fulfillment):
    serializer = PayloadSerializer()
    lines = FulfillmentLine.objects.prefetch_related(
        "order_line__variant__product__product_type", "stock"
    ).filter(fulfillment=fulfillment)
    line_fields = ("quantity",)
    return serializer.serialize(
        lines,
        fields=line_fields,
        extra_dict_data={
            "product_name": lambda fl: fl.order_line.product_name,
            "variant_name": lambda fl: fl.order_line.variant_name,
            "product_sku": lambda fl: fl.order_line.product_sku,
            "product_variant_id": lambda fl: fl.order_line.product_variant_id,
            "weight": (
                lambda fl: fl.order_line.variant.get_weight().g
                if fl.order_line.variant
                else None
            ),
            "weight_unit": "gram",
            "product_type": (
                lambda fl: fl.order_line.variant.product.product_type.name
                if fl.order_line.variant
                else None
            ),
            "unit_price_net": lambda fl: quantize_price(
                fl.order_line.unit_price_net_amount, fl.order_line.currency
            ),
            "unit_price_gross": lambda fl: quantize_price(
                fl.order_line.unit_price_gross_amount, fl.order_line.currency
            ),
            "undiscounted_unit_price_net": (
                lambda fl: quantize_price(
                    fl.order_line.undiscounted_unit_price.net.amount,
                    fl.order_line.currency,
                )
            ),
            "undiscounted_unit_price_gross": (
                lambda fl: quantize_price(
                    fl.order_line.undiscounted_unit_price.gross.amount,
                    fl.order_line.currency,
                )
            ),
            "total_price_net_amount": (
                lambda fl: quantize_price(
                    fl.order_line.undiscounted_unit_price.net.amount,
                    fl.order_line.currency,
                )
                * fl.quantity
            ),
            "total_price_gross_amount": (
                lambda fl: quantize_price(
                    fl.order_line.undiscounted_unit_price.gross.amount,
                    fl.order_line.currency,
                )
                * fl.quantity
            ),
            "currency": lambda fl: fl.order_line.currency,
            "warehouse_id": lambda fl: graphene.Node.to_global_id(
                "Warehouse", fl.stock.warehouse_id
            )
            if fl.stock
            else None,
            "sale_id": lambda fl: fl.order_line.sale_id,
            "voucher_code": lambda fl: fl.order_line.voucher_code,
        },
    )


@traced_payload_generator
def generate_fulfillment_payload(
    fulfillment: Fulfillment, requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()

    # fulfillment fields to serialize
    fulfillment_fields = (
        "status",
        "tracking_code",
        "order__user_email",
        "shipping_refund_amount",
        "total_refund_amount",
    )
    fulfillment_price_fields = (
        "shipping_refund_amount",
        "total_refund_amount",
    )
    order = fulfillment.order
    order_country = get_order_country(order)
    quantize_price_fields(
        fulfillment, fulfillment_price_fields, fulfillment.order.currency
    )
    fulfillment_line = fulfillment.lines.first()
    if fulfillment_line and fulfillment_line.stock:
        warehouse = fulfillment_line.stock.warehouse
    else:
        warehouse = Warehouse.objects.for_country(order_country).first()
    fulfillment_data = serializer.serialize(
        [fulfillment],
        fields=fulfillment_fields,
        additional_fields={
            "warehouse_address": (lambda f: warehouse.address, ADDRESS_FIELDS),
        },
        extra_dict_data={
            "order": json.loads(
                generate_order_payload(fulfillment.order, with_meta=False)
            )[0],
            "lines": json.loads(generate_fulfillment_lines_payload(fulfillment)),
            "meta": generate_meta(requestor_data=generate_requestor(requestor)),
        },
    )
    return fulfillment_data


@traced_payload_generator
def generate_page_payload(
    page: Page, requestor: Optional["RequestorOrLazyObject"] = None
):
    serializer = PayloadSerializer()
    page_fields = [
        "private_metadata",
        "metadata",
        "title",
        "content",
        "published_at",
        "is_published",
        "updated_at",
    ]
    page_payload = serializer.serialize(
        [page],
        fields=page_fields,
        extra_dict_data={
            "data": generate_meta(requestor_data=generate_requestor(requestor)),
            # deprecated in 3.3 - published_at should be used instead
            "publication_date": page.published_at,
        },
    )
    return page_payload


@traced_payload_generator
def generate_payment_payload(
    payment_data: "PaymentData", requestor: Optional["RequestorOrLazyObject"] = None
):
    data = asdict(payment_data)
    data["amount"] = quantize_price(data["amount"], data["currency"])
    payment_app_data = from_payment_app_id(data["gateway"])
    if payment_app_data:
        data["payment_method"] = payment_app_data.name
        data["meta"] = generate_meta(requestor_data=generate_requestor(requestor))
    return json.dumps(data, cls=CustomJsonEncoder)


@traced_payload_generator
def generate_list_gateways_payload(
    currency: Optional[str], checkout: Optional["Checkout"]
):
    if checkout:
        # Deserialize checkout payload to dict and generate a new payload including
        # currency.
        checkout_data = json.loads(generate_checkout_payload(checkout))[0]
    else:
        checkout_data = None
    payload = {"checkout": checkout_data, "currency": currency}
    return json.dumps(payload)


def _get_sample_object(qs: QuerySet):
    """Return random object from query."""
    random_object = qs.order_by("?").first()
    return random_object


def _remove_token_from_checkout(checkout):
    checkout_data = json.loads(checkout)
    checkout_data[0]["token"] = str(uuid.UUID(int=1))
    return json.dumps(checkout_data)


def _generate_sample_order_payload(event_name):
    order_qs = Order.objects.prefetch_related(
        "payments",
        "lines",
        "shipping_method",
        "shipping_address",
        "billing_address",
        "fulfillments",
    )
    order = None
    if event_name == WebhookEventAsyncType.ORDER_CREATED:
        order = _get_sample_object(order_qs.filter(status=OrderStatus.UNFULFILLED))
    elif event_name == WebhookEventAsyncType.ORDER_FULLY_PAID:
        order = _get_sample_object(
            order_qs.filter(payments__charge_status=ChargeStatus.FULLY_CHARGED)
        )
    elif event_name == WebhookEventAsyncType.ORDER_FULFILLED:
        order = _get_sample_object(
            order_qs.filter(fulfillments__status=FulfillmentStatus.FULFILLED)
        )
    elif event_name in [
        WebhookEventAsyncType.ORDER_CANCELLED,
        WebhookEventAsyncType.ORDER_UPDATED,
    ]:
        order = _get_sample_object(order_qs.filter(status=OrderStatus.CANCELED))
    if order:
        anonymized_order = anonymize_order(order)
        return generate_order_payload(anonymized_order)


@traced_payload_generator
def generate_sample_payload(event_name: str) -> Optional[dict]:
    checkout_events = [
        WebhookEventAsyncType.CHECKOUT_UPDATED,
        WebhookEventAsyncType.CHECKOUT_CREATED,
    ]
    pages_events = [
        WebhookEventAsyncType.PAGE_CREATED,
        WebhookEventAsyncType.PAGE_DELETED,
        WebhookEventAsyncType.PAGE_UPDATED,
    ]
    user_events = [
        WebhookEventAsyncType.CUSTOMER_CREATED,
        WebhookEventAsyncType.CUSTOMER_UPDATED,
    ]

    if event_name in user_events:
        user = generate_fake_user()
        payload = generate_customer_payload(user)
    elif event_name == WebhookEventAsyncType.PRODUCT_CREATED:
        product = _get_sample_object(
            Product.objects.prefetch_related("category", "collections", "variants")
        )
        payload = generate_product_payload(product) if product else None
    elif event_name in checkout_events:
        checkout = _get_sample_object(
            Checkout.objects.prefetch_related("lines__variant__product")
        )
        if checkout:
            anonymized_checkout = anonymize_checkout(checkout)
            checkout_payload = generate_checkout_payload(anonymized_checkout)
            payload = _remove_token_from_checkout(checkout_payload)
    elif event_name in pages_events:
        page = _get_sample_object(Page.objects.all())
        if page:
            payload = generate_page_payload(page)
    elif event_name == WebhookEventAsyncType.FULFILLMENT_CREATED:
        fulfillment = _get_sample_object(
            Fulfillment.objects.prefetch_related("lines__order_line__variant")
        )
        fulfillment.order = anonymize_order(fulfillment.order)
        payload = generate_fulfillment_payload(fulfillment)
    else:
        payload = _generate_sample_order_payload(event_name)
    return json.loads(payload) if payload else None


def process_translation_context(context):
    additional_id_fields = [
        ("product_id", "Product"),
        ("product_variant_id", "ProductVariant"),
        ("attribute_id", "Attribute"),
        ("page_id", "Page"),
        ("page_type_id", "PageType"),
    ]
    result = {}
    for key, type_name in additional_id_fields:
        if object_id := context.get(key, None):
            result[key] = graphene.Node.to_global_id(type_name, object_id)
        else:
            result[key] = None
    return result


@traced_payload_generator
def generate_translation_payload(
    translation: "Translation", requestor: Optional["RequestorOrLazyObject"] = None
):
    object_type, object_id = translation.get_translated_object_id()
    translated_keys = [
        {"key": key, "value": value}
        for key, value in translation.get_translated_keys().items()
    ]

    context = None
    if isinstance(translation, AttributeValueTranslation):
        context = process_translation_context(translation.get_translation_context())

    translation_data = {
        "id": graphene.Node.to_global_id(object_type, object_id),
        "language_code": translation.language_code,
        "type": object_type,
        "keys": translated_keys,
        "meta": generate_meta(requestor_data=generate_requestor(requestor)),
    }

    if context:
        translation_data.update(context)

    return json.dumps(translation_data)


def _generate_payload_for_shipping_method(method: ShippingMethodData):
    payload = {
        "id": method.graphql_id,
        "price": method.price.amount,
        "currency": method.price.currency,
        "name": method.name,
        "maximum_order_weight": method.maximum_order_weight,
        "minimum_order_weight": method.minimum_order_weight,
        "maximum_delivery_days": method.maximum_delivery_days,
        "minimum_delivery_days": method.minimum_delivery_days,
    }
    return payload


@traced_payload_generator
def generate_excluded_shipping_methods_for_order_payload(
    order: "Order",
    available_shipping_methods: List[ShippingMethodData],
):
    order_data = json.loads(generate_order_payload(order))[0]
    payload = {
        "order": order_data,
        "shipping_methods": [
            _generate_payload_for_shipping_method(shipping_method)
            for shipping_method in available_shipping_methods
        ],
    }
    return json.dumps(payload, cls=CustomJsonEncoder)


@traced_payload_generator
def generate_excluded_shipping_methods_for_checkout_payload(
    checkout: "Checkout",
    available_shipping_methods: List[ShippingMethodData],
):
    checkout_data = json.loads(generate_checkout_payload(checkout))[0]
    payload = {
        "checkout": checkout_data,
        "shipping_methods": [
            _generate_payload_for_shipping_method(shipping_method)
            for shipping_method in available_shipping_methods
        ],
    }
    return json.dumps(payload, cls=CustomJsonEncoder)


@traced_payload_generator
def generate_transaction_action_request_payload(
    transaction_data: "TransactionActionData",
    requestor: Optional["RequestorOrLazyObject"] = None,
) -> str:
    transaction = transaction_data.transaction

    action_value = (
        quantize_price(transaction_data.action_value, transaction.currency)
        if transaction_data.action_value
        else None
    )

    order_id = transaction.order_id
    graphql_order_id = (
        graphene.Node.to_global_id("Order", order_id) if order_id else None
    )

    checkout_id = transaction.checkout_id
    graphql_checkout_id = (
        graphene.Node.to_global_id("Checkout", checkout_id) if checkout_id else None
    )

    payload = {
        "action": {
            "type": transaction_data.action_type,
            "value": action_value,
            "currency": transaction.currency,
        },
        "transaction": {
            "status": transaction.status,
            "type": transaction.type,
            "reference": transaction.reference,
            "available_actions": transaction.available_actions,
            "currency": transaction.currency,
            "charged_value": quantize_price(
                transaction.charged_value, transaction.currency
            ),
            "authorized_value": quantize_price(
                transaction.authorized_value, transaction.currency
            ),
            "refunded_value": quantize_price(
                transaction.refunded_value, transaction.currency
            ),
            "voided_value": quantize_price(
                transaction.voided_value, transaction.currency
            ),
            "order_id": graphql_order_id,
            "checkout_id": graphql_checkout_id,
            "created_at": transaction.created_at,
            "modified_at": transaction.modified_at,
        },
        "meta": generate_meta(requestor_data=generate_requestor(requestor)),
    }
    return json.dumps(payload, cls=CustomJsonEncoder)
