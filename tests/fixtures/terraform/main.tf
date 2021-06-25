# https://github.com/gruntwork-io/terratest/tree/master/examples/terraform-hello-world-example

terraform {
  required_version = ">= 0.12.26"
}

variable "tag" {
  type        = string
}

resource "null_resource" "null" {
}

output "test_output" {
  value = "outputting ${var.tag}!"
}
