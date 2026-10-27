# HTTPS ALB with one hostname per service (<name>.<domain_name>), plus a TCP NLB for FIX.

data "aws_route53_zone" "this" {
  name         = var.hosted_zone_name
  private_zone = false
}

resource "aws_acm_certificate" "this" {
  domain_name       = "*.${var.domain_name}"
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.this.domain_validation_options : o.domain_name => o
  }
  zone_id         = data.aws_route53_zone.this.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# -- ALB ------------------------------------------------------------------------------------
resource "aws_lb" "alb" {
  name               = local.name
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
  # Desk Chat and the bot/inbox streams hold WebSockets open; don't cut quiet ones at 60 s.
  idle_timeout = 900
}

resource "aws_lb_target_group" "svc" {
  for_each             = local.services
  name                 = "${local.name}-${each.key}"
  port                 = each.value.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 5 # one task per service; nothing to drain to

  health_check {
    path                = each.value.health
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.alb.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this.certificate_arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"unknown host\"}"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.alb.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener_rule" "svc" {
  for_each     = local.services
  listener_arn = aws_lb_listener.https.arn
  priority     = 100 + index(keys(local.services), each.key)

  condition {
    host_header {
      values = [local.public_hosts[each.key]]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.svc[each.key].arn
  }
}

resource "aws_route53_record" "svc" {
  for_each = local.public_hosts
  zone_id  = data.aws_route53_zone.this.zone_id
  name     = each.value
  type     = "A"

  alias {
    name                   = aws_lb.alb.dns_name
    zone_id                = aws_lb.alb.zone_id
    evaluate_target_health = false
  }
}

# -- FIX NLB (plain TCP, like the local setup) ----------------------------------------------
resource "aws_lb" "fix" {
  name               = "${local.name}-fix"
  load_balancer_type = "network"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.nlb.id]
}

resource "aws_lb_target_group" "fix" {
  name                 = "${local.name}-fix"
  port                 = local.fix_port
  protocol             = "TCP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 5

  health_check {
    protocol = "HTTP"
    port     = tostring(local.mocks.ngt.port)
    path     = "/health"
    interval = 15
  }
}

resource "aws_lb_listener" "fix" {
  load_balancer_arn = aws_lb.fix.arn
  port              = local.fix_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.fix.arn
  }
}

resource "aws_route53_record" "fix" {
  zone_id = data.aws_route53_zone.this.zone_id
  name    = local.fix_host
  type    = "A"

  alias {
    name                   = aws_lb.fix.dns_name
    zone_id                = aws_lb.fix.zone_id
    evaluate_target_health = false
  }
}
