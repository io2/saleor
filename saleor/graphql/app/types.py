from typing import List

import graphene

from ...app import models
from ...app.types import AppExtensionTarget
from ...core.exceptions import PermissionDenied
from ...core.jwt import JWT_THIRDPARTY_ACCESS_TYPE
from ...core.permissions import (
    AppPermission,
    AuthorizationFilters,
    message_one_of_permissions_required,
)
from ..account.utils import is_owner_or_has_one_of_perms
from ..core.connection import CountableConnection
from ..core.descriptions import (
    ADDED_IN_31,
    ADDED_IN_35,
    DEPRECATED_IN_3X_FIELD,
    PREVIEW_FEATURE,
)
from ..core.federation import federated_entity, resolve_federation_references
from ..core.types import Job, ModelObjectType, NonNullList, Permission
from ..core.utils import from_global_id_or_error
from ..meta.types import ObjectWithMetadata
from ..utils import format_permissions_for_display, get_user_or_app_from_context
from ..webhook.types import Webhook
from .dataloaders import AppByIdLoader, AppExtensionByAppIdLoader
from .enums import AppExtensionMountEnum, AppExtensionTargetEnum, AppTypeEnum
from .resolvers import (
    resolve_access_token_for_app,
    resolve_access_token_for_app_extension,
    resolve_app_extension_url,
)


def has_required_permission(app: models.App, context):
    requester = get_user_or_app_from_context(context)
    if not is_owner_or_has_one_of_perms(requester, app, AppPermission.MANAGE_APPS):
        raise PermissionDenied(
            permissions=[AppPermission.MANAGE_APPS, AuthorizationFilters.OWNER]
        )


def check_permission_for_access_to_meta(app: models.App, info):
    has_access = has_access_to_app_public_meta(app, info)
    if not has_access:
        raise PermissionDenied(
            permissions=[AppPermission.MANAGE_APPS, AuthorizationFilters.OWNER]
        )


def has_access_to_app_public_meta(root, info) -> bool:
    auth_token = info.context.decoded_auth_token or {}
    if auth_token.get("type") == JWT_THIRDPARTY_ACCESS_TYPE:
        _, app_id = from_global_id_or_error(auth_token["app"], "App")
    else:
        app_id = info.context.app.id if info.context.app else None
    if app_id is not None and int(app_id) == root.id:
        return True
    requester = get_user_or_app_from_context(info.context)
    return requester.has_perm(AppPermission.MANAGE_APPS)


class AppManifestExtension(graphene.ObjectType):
    permissions = NonNullList(
        Permission,
        description="List of the app extension's permissions.",
        required=True,
    )
    label = graphene.String(
        description="Label of the extension to show in the dashboard.", required=True
    )
    url = graphene.String(
        description="URL of a view where extension's iframe is placed.", required=True
    )
    mount = AppExtensionMountEnum(
        description="Place where given extension will be mounted.",
        required=True,
    )
    target = AppExtensionTargetEnum(
        description="Type of way how app extension will be opened.", required=True
    )

    @staticmethod
    def resolve_target(root, info):
        return root.get("target") or AppExtensionTarget.POPUP

    @staticmethod
    def resolve_url(root, info):
        """Return an extension URL."""
        return resolve_app_extension_url(root)


class AppExtension(AppManifestExtension, ModelObjectType):
    id = graphene.GlobalID(required=True)
    app = graphene.Field("saleor.graphql.app.types.App", required=True)
    access_token = graphene.String(
        description="JWT token used to authenticate by thridparty app extension."
    )

    class Meta:
        description = "Represents app data."
        interfaces = [graphene.relay.Node]
        model = models.AppExtension

    @staticmethod
    def resolve_url(root, info):
        return (
            AppByIdLoader(info.context)
            .load(root.app_id)
            .then(
                lambda app: AppManifestExtension.resolve_url(
                    {"target": root.target, "app_url": app.app_url, "url": root.url},
                    info,
                )
            )
        )

    @staticmethod
    def resolve_target(root, info):
        return root.target

    @staticmethod
    def resolve_app(root, info):
        app_id = None
        app = info.context.app
        if app and app.id == root.app_id:
            app_id = root.app_id
        else:
            requestor = get_user_or_app_from_context(info.context)
            if requestor.has_perm(AppPermission.MANAGE_APPS):
                app_id = root.app_id

        if not app_id:
            raise PermissionDenied(
                permissions=[AppPermission.MANAGE_APPS, AuthorizationFilters.OWNER]
            )
        return AppByIdLoader(info.context).load(app_id)

    @staticmethod
    def resolve_permissions(root: models.AppExtension, _info):
        permissions = root.permissions.prefetch_related("content_type").order_by(
            "codename"
        )
        return format_permissions_for_display(permissions)

    @staticmethod
    def resolve_access_token(root: models.App, info):
        return resolve_access_token_for_app_extension(info, root)


class AppExtensionCountableConnection(CountableConnection):
    class Meta:
        node = AppExtension


class Manifest(graphene.ObjectType):
    identifier = graphene.String(required=True)
    version = graphene.String(required=True)
    name = graphene.String(required=True)
    about = graphene.String()
    permissions = NonNullList(Permission)
    app_url = graphene.String()
    configuration_url = graphene.String(
        description="URL to iframe with the configuration for the app.",
        deprecation_reason=f"{DEPRECATED_IN_3X_FIELD} Use `appUrl` instead.",
    )
    token_target_url = graphene.String()
    data_privacy = graphene.String(
        description="Description of the data privacy defined for this app.",
        deprecation_reason=f"{DEPRECATED_IN_3X_FIELD} Use `dataPrivacyUrl` instead.",
    )
    data_privacy_url = graphene.String()
    homepage_url = graphene.String()
    support_url = graphene.String()
    extensions = NonNullList(AppManifestExtension, required=True)

    class Meta:
        description = "The manifest definition."

    @staticmethod
    def resolve_extensions(root, info):
        for extension in root.extensions:
            extension["app_url"] = root.app_url
        return root.extensions


class AppToken(graphene.ObjectType):
    id = graphene.GlobalID(required=True)
    name = graphene.String(description="Name of the authenticated token.")
    auth_token = graphene.String(description="Last 4 characters of the token.")

    class Meta:
        description = "Represents token data."
        interfaces = [graphene.relay.Node]
        permissions = (AppPermission.MANAGE_APPS,)

    @staticmethod
    def get_node(info, id):
        try:
            return models.AppToken.objects.get(pk=id)
        except models.AppToken.DoesNotExist:
            return None

    @staticmethod
    def resolve_auth_token(root: models.AppToken, _info):
        return root.token_last_4


@federated_entity("id")
class App(ModelObjectType):
    id = graphene.GlobalID(required=True)
    permissions = NonNullList(Permission, description="List of the app's permissions.")
    created = graphene.DateTime(
        description="The date and time when the app was created."
    )
    is_active = graphene.Boolean(
        description="Determine if app will be set active or not."
    )
    name = graphene.String(description="Name of the app.")
    type = AppTypeEnum(description="Type of the app.")
    tokens = NonNullList(
        AppToken,
        description=(
            "Last 4 characters of the tokens."
            + message_one_of_permissions_required(
                [AppPermission.MANAGE_APPS, AuthorizationFilters.OWNER]
            )
        ),
    )
    webhooks = NonNullList(
        Webhook,
        description=(
            "List of webhooks assigned to this app."
            + message_one_of_permissions_required(
                [AppPermission.MANAGE_APPS, AuthorizationFilters.OWNER]
            )
        ),
    )

    about_app = graphene.String(description="Description of this app.")

    data_privacy = graphene.String(
        description="Description of the data privacy defined for this app.",
        deprecation_reason=f"{DEPRECATED_IN_3X_FIELD} Use `dataPrivacyUrl` instead.",
    )
    data_privacy_url = graphene.String(
        description="URL to details about the privacy policy on the app owner page."
    )
    homepage_url = graphene.String(description="Homepage of the app.")
    support_url = graphene.String(description="Support page for the app.")
    configuration_url = graphene.String(
        description="URL to iframe with the configuration for the app.",
        deprecation_reason=f"{DEPRECATED_IN_3X_FIELD} Use `appUrl` instead.",
    )
    app_url = graphene.String(description="URL to iframe with the app.")
    manifest_url = graphene.String(
        description="URL to manifest used during app's installation." + ADDED_IN_35
    )
    version = graphene.String(description="Version number of the app.")
    access_token = graphene.String(
        description="JWT token used to authenticate by thridparty app."
    )
    extensions = NonNullList(
        AppExtension,
        description="App's dashboard extensions." + ADDED_IN_31 + PREVIEW_FEATURE,
        required=True,
    )

    class Meta:
        description = "Represents app data."
        interfaces = [graphene.relay.Node, ObjectWithMetadata]
        model = models.App

    @staticmethod
    def resolve_created(root: models.App, _info):
        return root.created_at

    @staticmethod
    def resolve_permissions(root: models.App, _info):
        permissions = root.permissions.prefetch_related("content_type").order_by(
            "codename"
        )
        return format_permissions_for_display(permissions)

    @staticmethod
    def resolve_tokens(root: models.App, info):
        has_required_permission(root, info.context)
        return root.tokens.all()

    @staticmethod
    def resolve_webhooks(root: models.App, info):
        has_required_permission(root, info.context)
        return root.webhooks.all()

    @staticmethod
    def resolve_access_token(root: models.App, info):
        return resolve_access_token_for_app(info, root)

    @staticmethod
    def resolve_extensions(root: models.App, info):
        return AppExtensionByAppIdLoader(info.context).load(root.id)

    @staticmethod
    def __resolve_references(roots: List["App"], info):
        from .resolvers import resolve_apps

        requestor = get_user_or_app_from_context(info.context)
        if not requestor.has_perm(AppPermission.MANAGE_APPS):
            qs = models.App.objects.none()
        else:
            qs = resolve_apps(info)

        return resolve_federation_references(App, roots, qs)

    @staticmethod
    def resolve_metadata(root: models.App, info):
        check_permission_for_access_to_meta(root, info)
        return ObjectWithMetadata.resolve_metadata(root, info)

    @staticmethod
    def resolve_metafield(root: models.App, info, *, key: str):
        check_permission_for_access_to_meta(root, info)
        return ObjectWithMetadata.resolve_metafield(root, info, key=key)

    @staticmethod
    def resolve_metafields(root: models.App, info, *, keys=None):
        check_permission_for_access_to_meta(root, info)
        return ObjectWithMetadata.resolve_metafields(root, info, keys=keys)


class AppCountableConnection(CountableConnection):
    class Meta:
        node = App


class AppInstallation(ModelObjectType):
    id = graphene.GlobalID(required=True)
    app_name = graphene.String(required=True)
    manifest_url = graphene.String(required=True)

    class Meta:
        model = models.AppInstallation
        description = "Represents ongoing installation of app."
        interfaces = [graphene.relay.Node, Job]
