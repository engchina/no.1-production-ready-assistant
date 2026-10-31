data "oci_core_subnet" "selected_compute_subnet" {
  subnet_id = var.subnet_ai_subnet_id
}
