import os
import torch


def adjust_learning_rate(optimizer, lr):
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def save_checkpoint(dir, epoch, **kwargs):
    state = {
        'epoch': epoch,
    }
    state.update(kwargs)
    filepath = os.path.join(dir, 'checkpoint-%d.pt' % epoch)
    torch.save(state, filepath)


def train_epoch(loader, model, criterion, optimizer, weight_quantizer, grad_quantizer,
                writer, epoch, quant_bias=True, quant_bn=True, log_error=False):
    loss_sum = 0.0
    correct = 0.0

    model.train()
    ttl = 0
    for i, (input, target) in enumerate(loader):
        input = input.cuda(async=True)
        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input)
        target_var = torch.autograd.Variable(target)

        output = model(input_var)
        loss = criterion(output, target_var)

        optimizer.zero_grad()
        loss.backward()

        # Write parameters
        # if log_error and i==0:
        #     for name, param in model.named_parameters():
        #         writer.add_histogram(
        #             "param-before/%s"%name, param.clone().cpu().data.numpy(), epoch)
        #         writer.add_histogram(
        #             "gradient-before/%s"%name, param.grad.clone().cpu().data.numpy(), epoch)

        # gradient quantization
        if grad_quantizer != None:
            for name, p in model.named_parameters():
                if 'bn' in name.split(".")[-2] and not quant_bn:
                    continue
                if 'bias' in name.split(".")[-1] and not quant_bias:
                    continue
                p.grad.data = grad_quantizer(p.grad.data).data

        optimizer.step()

        # Weight quantization
        if weight_quantizer != None:
            for name, p in model.named_parameters():
                if 'bn' in name.split(".")[-2] and not quant_bn:
                    continue
                if 'bias' in name.split(".")[-1] and not quant_bias:
                    continue
                 # log quantization error at the first batch every epoch
                if log_error and i == 0:
                    data_quant = weight_quantizer(p.data).data
                    error = torch.nn.functional.mse_loss(p.data, data_quant)
                    p.data = data_quant
                    writer.add_scalar(
                        "param-quantize_error/%s"%name, error.cpu().data.numpy(), epoch)
                else:
                    p.data = weight_quantizer(p.data).data

        # Write parameters after quantization
        # if log_error and i == 0:
        #     for name, param in model.named_parameters():
        #         writer.add_histogram(
        #             "param-after/%s"%name, param.clone().cpu().data.numpy(), epoch)
        #         writer.add_histogram(
        #             "gradient-after/%s"%name, param.grad.clone().cpu().data.numpy(), epoch)

        # loss_sum += loss.data[0] * input.size(0)
        loss_sum += loss.cpu().item() * input.size(0)
        pred = output.data.max(1, keepdim=True)[1]
        correct += pred.eq(target_var.data.view_as(pred)).sum()
        ttl += input.size()[0]

    correct = correct.cpu().item()
    # print("Correct:%s/%s, %s"%(correct, len(loader.dataset), ttl))
    return {
        'loss': loss_sum / float(ttl),
        'accuracy': correct / float(ttl) * 100.0,
    }


def eval(loader, model, criterion):
    loss_sum = 0.0
    correct = 0.0

    model.eval()
    cnt = 0
    with torch.no_grad():
        for i, (input, target) in enumerate(loader):
            input = input.cuda(async=True)
            target = target.cuda(async=True)
            input_var = torch.autograd.Variable(input)
            target_var = torch.autograd.Variable(target)

            output = model(input_var)
            loss = criterion(output, target_var)

            loss_sum += loss.data.cpu().item() * input.size(0)
            pred = output.data.max(1, keepdim=True)[1]
            correct += pred.eq(target_var.data.view_as(pred)).sum()
            cnt += int(input.size()[0])

    correct = correct.cpu().item()
    # print("Correct:%s/%s, %s"%(correct, len(loader.dataset), cnt))
    return {
        'loss': loss_sum / float(cnt),
        'accuracy': correct / float(cnt) * 100.0,
    }


def moving_average(net1, net2, alpha=1):
    for param1, param2 in zip(net1.parameters(), net2.parameters()):
        param1.data *= (1.0 - alpha)
        param1.data += param2.data * alpha


def _check_bn(module, flag):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        flag[0] = True


def check_bn(model):
    flag = [False]
    model.apply(lambda module: _check_bn(module, flag))
    return flag[0]


def reset_bn(module):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        module.running_mean = torch.zeros_like(module.running_mean)
        module.running_var = torch.ones_like(module.running_var)


def _get_momenta(module, momenta):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        momenta[module] = module.momentum


def _set_momenta(module, momenta):
    if issubclass(module.__class__, torch.nn.modules.batchnorm._BatchNorm):
        module.momentum = momenta[module]


def bn_update(loader, model):
    """
        BatchNorm buffers update (if any).
        Performs 1 epochs to estimate buffers average using train dataset.

        :param loader: train dataset loader for buffers average estimation.
        :param model: model being update
        :return: None
    """
    if not check_bn(model):
        return
    model.train()
    momenta = {}
    model.apply(reset_bn)
    model.apply(lambda module: _get_momenta(module, momenta))
    n = 0
    for input, _ in loader:
        input = input.cuda(async=True)
        input_var = torch.autograd.Variable(input)
        b = input_var.data.size(0)

        momentum = b / (n + b)
        for module in momenta.keys():
            module.momentum = momentum

        model(input_var)
        n += b

    model.apply(lambda module: _set_momenta(module, momenta))