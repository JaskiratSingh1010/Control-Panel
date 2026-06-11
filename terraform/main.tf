terraform {
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

variable "azure_storage_connection_string" {
  default = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=test;BlobEndpoint=http://localhost:4577/devstoreaccount1;QueueEndpoint=http://localhost:4577/devstoreaccount1;"
}

resource "null_resource" "create_blob_container" {
  provisioner "local-exec" {
    command = "az storage container create --name control-panel-static --connection-string \"${var.azure_storage_connection_string}\""
  }
}

resource "null_resource" "create_queue" {
  provisioner "local-exec" {
    command = "az storage queue create --name control-panel-queue --connection-string \"${var.azure_storage_connection_string}\""
  }
}