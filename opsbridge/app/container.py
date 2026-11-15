"""Composition root: the ONLY place that decides between real and mock integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.database.session import Database
from app.integrations.jira_client import HttpJiraClient, JiraClient, MockJiraClient
from app.integrations.microsoft_graph import (
    ClientCredentialsTokenProvider,
    GraphClient,
    HttpGraphClient,
    MissingDelegatedTokenProvider,
    MockGraphClient,
    RefreshTokenProvider,
)
from app.services.command_handler import CommandConfig, CommandHandler, CommandIntake
from app.services.command_parser import CommandParser
from app.services.ingestion_service import Dispatcher, IngestionService
from app.services.jira_sync_service import DeliveryConfig, JiraSyncService, SyncConfig, ThreadUpdateNotifier
from app.services.jira_service import JiraService
from app.services.message_processor import MessageProcessor, ProcessorConfig
from app.services.notification_service import NotificationService
from app.services.request_parser import RequestParser, TemplateRequestParser
from app.services.request_validator import RequestValidator
from app.services.subscription_service import SubscriptionService
from app.services.support_request_service import SupportRequestService
from app.workers.background import BackgroundWorker, SyncDispatcher


@dataclass
class Container:
    settings: Settings
    db: Database
    graph: GraphClient
    jira_client: JiraClient
    jira: JiraService
    parser: RequestParser
    processor: MessageProcessor
    commands: CommandHandler
    command_intake: CommandIntake
    jira_sync: JiraSyncService
    thread_updates: ThreadUpdateNotifier
    dispatcher: Any  # BackgroundWorker | SyncDispatcher
    ingestion: IngestionService
    subscriptions: SubscriptionService
    support_requests: SupportRequestService

    def close(self) -> None:
        if isinstance(self.dispatcher, BackgroundWorker):
            self.dispatcher.stop()
        self.graph.close()
        self.jira_client.close()
        self.db.dispose()


def build_graph_client(settings: Settings) -> GraphClient:
    if settings.mock_mode:
        return MockGraphClient(service_account_user_id=settings.microsoft_service_account_user_id
                               or "mock-opsbridge-service-account")
    token_http = httpx.Client(timeout=httpx.Timeout(settings.http_timeout_seconds))
    app_tokens = ClientCredentialsTokenProvider(
        tenant_id=settings.microsoft_tenant_id,
        client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret.get_secret_value(),
        login_base_url=settings.login_base_url,
        http=token_http,
        max_attempts=settings.http_max_attempts,
    )
    refresh = settings.microsoft_delegated_refresh_token.get_secret_value()
    delegated = (
        RefreshTokenProvider(
            tenant_id=settings.microsoft_tenant_id,
            client_id=settings.microsoft_client_id,
            refresh_token=refresh,
            login_base_url=settings.login_base_url,
            http=token_http,
            max_attempts=settings.http_max_attempts,
        )
        if refresh
        else MissingDelegatedTokenProvider()
    )
    return HttpGraphClient(
        app_tokens=app_tokens,
        delegated_tokens=delegated,
        base_url=settings.graph_base_url,
        timeout=settings.http_timeout_seconds,
        max_attempts=settings.http_max_attempts,
    )


def build_jira_client(settings: Settings) -> JiraClient:
    if settings.mock_mode:
        return MockJiraClient(base_url=settings.effective_jira_base_url, project_key=settings.jira_project_key)
    return HttpJiraClient(
        base_url=settings.jira_base_url,
        email=settings.jira_email,
        api_token=settings.jira_api_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
        max_attempts=settings.http_max_attempts,
    )


def build_container(
    settings: Settings,
    *,
    db: Database | None = None,
    graph: GraphClient | None = None,
    jira_client: JiraClient | None = None,
    parser: RequestParser | None = None,
    sync_dispatch: bool | None = None,
) -> Container:
    db = db or Database(settings.database_url)
    graph = graph or build_graph_client(settings)
    jira_client = jira_client or build_jira_client(settings)
    parser = parser or TemplateRequestParser()  # Phase 3: select an LLM parser here
    service_account = settings.microsoft_service_account_user_id or (
        getattr(graph, "service_account_user_id", None) if settings.mock_mode else None
    )

    jira = JiraService(
        jira_client,
        project_key=settings.jira_project_key,
        issue_type=settings.jira_issue_type,
        priority_map=settings.jira_priority_map,
        extra_labels=settings.jira_extra_labels,
    )
    processor = MessageProcessor(
        db=db,
        graph=graph,
        parser=parser,
        validator=RequestValidator(),
        jira=jira,
        notifier=NotificationService(graph),
        config=ProcessorConfig(
            team_id=settings.effective_team_id,
            channel_id=settings.effective_channel_id,
            service_account_user_id=service_account,
            worker_lease_seconds=settings.worker_lease_seconds,
            max_retries=settings.max_retries,
            retry_base_delay_seconds=settings.retry_base_delay_seconds,
            retry_max_delay_seconds=settings.retry_max_delay_seconds,
            jira_reconcile_delay_seconds=settings.jira_reconcile_delay_seconds,
        ),
    )
    notifier = processor.notifier
    commands = CommandHandler(
        db=db,
        graph=graph,
        jira=jira_client,
        notifier=notifier,
        parser=CommandParser(),
        config=CommandConfig(
            service_account_user_id=service_account,
            admin_user_ids=settings.command_admin_user_ids,
            close_status=settings.jira_close_status,
            reopen_status=settings.jira_reopen_status,
            escalation_label=settings.jira_escalation_label,
            priority_map=settings.jira_priority_map,
            escalation_team_id=settings.effective_team_id,
            escalation_channel_id=settings.effective_escalation_channel_id,
            worker_lease_seconds=settings.worker_lease_seconds,
            max_retries=settings.max_retries,
            retry_base_delay_seconds=settings.retry_base_delay_seconds,
            retry_max_delay_seconds=settings.retry_max_delay_seconds,
        ),
    )
    thread_updates = ThreadUpdateNotifier(
        db=db,
        notifier=notifier,
        config=DeliveryConfig(
            worker_lease_seconds=settings.worker_lease_seconds,
            max_retries=settings.max_retries,
            retry_base_delay_seconds=settings.retry_base_delay_seconds,
            retry_max_delay_seconds=settings.retry_max_delay_seconds,
        ),
    )
    subscriptions = SubscriptionService(
        db=db,
        graph=graph,
        team_id=settings.effective_team_id,
        channel_id=settings.effective_channel_id,
        base_url=settings.app_base_url or "http://localhost:8000",
        client_state=settings.effective_client_state,
        lifetime_minutes=settings.graph_subscription_lifetime_minutes,
        renewal_threshold_minutes=settings.graph_subscription_renewal_threshold_minutes,
    )
    use_sync = (not settings.background_worker_enabled) if sync_dispatch is None else sync_dispatch
    dispatcher: Dispatcher = (
        SyncDispatcher(processor, commands, thread_updates)
        if use_sync
        else BackgroundWorker(
            db=db,
            processor=processor,
            subscriptions=subscriptions,
            commands=commands,
            notifications=thread_updates,
            worker_count=settings.worker_count,
            poll_interval=settings.worker_poll_interval_seconds,
            subscription_check_interval=settings.subscription_check_interval_seconds,
        )
    )
    jira_sync = JiraSyncService(
        db=db,
        jira=jira_client,
        dispatcher=dispatcher,
        config=SyncConfig(
            enabled=settings.jira_sync_enabled,
            project_key=settings.jira_project_key,
            jira_base_url=settings.effective_jira_base_url,
            close_status=settings.jira_close_status,
            poll_interval_seconds=settings.jira_sync_poll_interval_seconds,
        ),
    )
    if isinstance(dispatcher, BackgroundWorker) and settings.jira_sync_enabled:
        dispatcher.jira_sync = jira_sync
        dispatcher.jira_poll_interval = settings.jira_sync_poll_interval_seconds
    command_intake = CommandIntake(db=db, dispatcher=dispatcher)
    processor.on_reply = command_intake.accept_message  # route fetched replies to command handling
    ingestion = IngestionService(
        db=db,
        dispatcher=dispatcher,
        command_intake=command_intake,
        team_id=settings.effective_team_id,
        channel_id=settings.effective_channel_id,
        client_state=settings.effective_client_state,
        tenant_id=settings.microsoft_tenant_id or None,
    )
    return Container(
        settings=settings,
        db=db,
        graph=graph,
        jira_client=jira_client,
        jira=jira,
        parser=parser,
        processor=processor,
        commands=commands,
        command_intake=command_intake,
        jira_sync=jira_sync,
        thread_updates=thread_updates,
        dispatcher=dispatcher,
        ingestion=ingestion,
        subscriptions=subscriptions,
        support_requests=SupportRequestService(db=db, dispatcher=dispatcher),
    )
