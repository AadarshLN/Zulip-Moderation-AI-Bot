# Install Terraform
brew install terraform   # macOS; or download from terraform.io

# Install Ansible + required collections
pip install ansible
ansible-galaxy collection install community.general ansible.posix
#Add to PATH if required

# Install OpenStack CLI (for creating object store containers)
pip install python-openstackclient

# Confirm your Chameleon SSH key exists
ls ~/.ssh/id_rsa_chameleon   # if named differently, update var.key in terraform

OpenStack credentials (~/.config/openstack/clouds.yaml):
Download from the Chameleon dashboard → Identity → Application Credentials → Download clouds.yaml and place it at ~/.config/openstack/clouds.yaml.

Assume CHI@TACC bare metal instance 

Phase 1 — Terraform (provision VM)
Run from infra/terraform/ on your local machine:


cd infra/terraform
terraform init

# Plan first to review what will be created
terraform plan \
  -var="suffix=proj09" \
  -var="reservation_id=xxxxxxxx-60d5-xxxx-ba6a-bb733aec98e7"

# Apply (will prompt for confirmation)
terraform apply \
  -var="suffix=proj09" \
  -var="reservation_id=xxxxxxxx-60d5-xxxx-ba6a-bb733aec98e7"

# Save the floating IP — you'll need it for every Ansible run
export ANSIBLE_HOST=$(terraform output -raw floating_ip_out)
echo $ANSIBLE_HOST   # e.g. 129.114.x.x

