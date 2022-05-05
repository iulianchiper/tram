import io
import json
import logging
from urllib.parse import quote

from constance import config
from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from rest_framework import renderers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

import tram.report.docx
from tram import serializers
from tram.ml import base
from tram.models import (
    AttackObject,
    Document,
    DocumentProcessingJob,
    Mapping,
    Report,
    Sentence,
)

logger = logging.getLogger(__name__)


class AttackObjectViewSet(viewsets.ModelViewSet):
    queryset = AttackObject.objects.all()
    serializer_class = serializers.AttackObjectSerializer


class DocumentProcessingJobViewSet(viewsets.ModelViewSet):
    queryset = DocumentProcessingJob.objects.all()
    serializer_class = serializers.DocumentProcessingJobSerializer


class MappingViewSet(viewsets.ModelViewSet):
    queryset = Mapping.objects.all()
    serializer_class = serializers.MappingSerializer

    def get_queryset(self):
        queryset = MappingViewSet.queryset
        sentence_id = self.request.query_params.get("sentence-id", None)
        if sentence_id:
            queryset = queryset.filter(sentence__id=sentence_id)

        return queryset


class ReportViewSet(viewsets.ModelViewSet):
    queryset = Report.objects.all()
    serializer_class = serializers.ReportSerializer

    @action(detail=True, name="Export JSON format")
    def json(self, request, pk):
        """
        Export a report into JSON format.

        This is designed to be called from a browser, so it ignores the
        negotiated content type and forces JSON rendering.

        :param request: provided by Django
        :param pk: provided by Django
        """
        report = self.get_object()
        filename = quote(report.name, safe="") + ".json"
        serializer = serializers.ReportExportSerializer(instance=report)
        request.accepted_renderer = renderers.JSONRenderer()
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return Response(
            serializer.data,
            content_type="application/json",
            headers=headers,
        )

    @action(detail=True, name="Export DOCX format")
    def docx(self, request, pk):
        """
        Export a report into Word .docx format.

        This is designed to be called from a browser, so it ignores the
        negotiated content type and forces .docx rendering.

        :param request: provided by Django
        :param pk: provided by Django
        """
        report = self.get_object()
        filename = quote(report.name, safe="") + ".docx"
        serializer = serializers.ReportExportSerializer(instance=report)
        request.accepted_renderer = renderers.JSONRenderer()
        document = tram.report.docx.build(serializer.data)
        buffer = io.BytesIO()
        document.save(buffer)
        buffer.seek(0)
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Encoding": "UTF-8",
        }
        return StreamingHttpResponse(
            streaming_content=buffer,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers=headers,
        )


class SentenceViewSet(viewsets.ModelViewSet):
    queryset = Sentence.objects.all()
    serializer_class = serializers.SentenceSerializer

    def get_queryset(self):
        queryset = SentenceViewSet.queryset
        report_id = self.request.query_params.get("report-id", None)
        if report_id:
            queryset = queryset.filter(report__id=report_id)

        attack_id = self.request.query_params.get("attack-id", None)
        if attack_id:
            sentences = Mapping.objects.filter(
                attack_object__attack_id=attack_id
            ).values("sentence")
            queryset = queryset.filter(id__in=sentences)
        return queryset


@login_required
def index(request):
    jobs = DocumentProcessingJob.objects.all()
    job_serializer = serializers.DocumentProcessingJobSerializer(jobs, many=True)

    reports = Report.objects.all()
    report_serializer = serializers.ReportSerializer(reports, many=True)

    context = {
        "job_queue": job_serializer.data,
        "reports": report_serializer.data,
    }

    return render(request, "index.html", context=context)


@login_required
def upload(request):
    """Places a file into ml-pipeline for analysis"""
    if request.method != "POST":
        return HttpResponse("Request method must be POST", status=405)

    # Initialize the processing job.
    dpj = None

    # Initialize response.
    response = {"message": "File saved for processing."}

    file_content_type = request.FILES["file"].content_type
    if file_content_type in (
        "application/pdf",  # .pdf files
        "text/html",  # .html files
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx files
        "text/plain",  # .txt files
    ):
        dpj = DocumentProcessingJob.create_from_file(
            request.FILES["file"], request.user
        )
    elif file_content_type in ("application/json",):  # .json files
        json_data = json.loads(request.FILES["file"].read())
        res = serializers.ReportExportSerializer(data=json_data)

        if res.is_valid():
            res.save(created_by=request.user)
        else:
            return HttpResponseBadRequest(res.errors)
    else:
        return HttpResponseBadRequest("Unsupported file type")

    if dpj:
        response["job-id"] = dpj.pk
        response["doc-id"] = dpj.document.pk

    return JsonResponse(response)


@login_required
def ml_home(request):
    techniques = AttackObject.get_sentence_counts()
    model_metadata = base.ModelManager.get_all_model_metadata()

    context = {
        "techniques": techniques,
        "ML_ACCEPT_THRESHOLD": config.ML_ACCEPT_THRESHOLD,
        "ML_CONFIDENCE_THRESHOLD": config.ML_CONFIDENCE_THRESHOLD,
        "models": model_metadata,
    }

    return render(request, "ml_home.html", context)


@login_required
def ml_technique_sentences(request, attack_id):
    techniques = AttackObject.objects.all().order_by("attack_id")
    techniques_serializer = serializers.AttackObjectSerializer(techniques, many=True)

    context = {"attack_id": attack_id, "attack_techniques": techniques_serializer.data}
    return render(request, "technique_sentences.html", context)


@login_required
def ml_model_detail(request, model_key):
    try:
        model_metadata = base.ModelManager.get_model_metadata(model_key)
    except ValueError:
        raise Http404("Model does not exists")
    context = {"model": model_metadata}
    return render(request, "model_detail.html", context)


@login_required
def analyze(request, pk):
    report = Report.objects.get(id=pk)
    techniques = AttackObject.objects.all().order_by("attack_id")
    techniques_serializer = serializers.AttackObjectSerializer(techniques, many=True)

    context = {
        "report_id": report.id,
        "report_name": report.name,
        "attack_techniques": techniques_serializer.data,
    }
    return render(request, "analyze.html", context)


@login_required
def download_document(request, doc_id):
    """Download a verbatim copy of a previously uploaded document."""
    doc = Document.objects.get(id=doc_id)
    docfile = doc.docfile

    try:
        with docfile.open("rb") as report_file:
            response = HttpResponse(
                report_file, content_type="application/octet-stream"
            )
            filename = quote(docfile.name)
            response["Content-Disposition"] = f"attachment; filename={filename}"
    except IOError:
        raise Http404("File does not exist")

    return response
