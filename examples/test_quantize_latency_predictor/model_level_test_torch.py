import os
import json
import time
import torch
from torch import nn
# from tensorflow import keras
from nn_meter.dataset.bench_dataset import latency_metrics
from nn_meter.builder.backends import connect_backend
from nn_meter.predictor import load_latency_predictor
from nn_meter.builder import builder_config
from nn_meter.builder.nn_generator.torch_networks.utils import get_inputs_by_shapes

from nas_models.networks.torch.mobilenetv3 import MobileNetV3Net
from nas_models.networks.torch.resnet import ResNetNet
from nas_models.blocks.torch.mobilenetv3_block import SE


output_path = "/data/jiahang/working/nn-Meter/examples/test_quantize_latency_predictor"
output_name = os.path.join(output_path, "MobilenetV3_test.onnx")

workspace = "/sdc/jiahang/working/ort_mobilenetv3_workspace"
builder_config.init(workspace)
backend = connect_backend(backend_name='ort_cpu_int8')
predictor_name = "onnxruntime_int8"
predictor = load_latency_predictor(predictor_name)


def profile_and_predict(model, input_shape, mark = "", model_pred = None):
    # print("\n")
    # print(model)
    # input_shape example [3, 224, 224]
    torch.onnx.export(
            model,
            get_inputs_by_shapes([[*input_shape]], 1),
            output_name,
            input_names=['input'],
            output_names=['output'],
            verbose=False,
            export_params=True,
            opset_version=12,
            do_constant_folding=True,
        )
    res = backend.profile_model_file(output_name, output_path, input_shape=[[*input_shape]])
    if model_pred != None:
        pred_lat = predictor.predict(model_pred, "torch", input_shape=tuple([1] + input_shape), apply_nni=False) # in unit of ms
    else:
        pred_lat = predictor.predict(model, "torch", input_shape=tuple([1] + input_shape), apply_nni=False) # in unit of ms
    # print(f"[{mark}]: ", "profiled: ", res["latency"].avg, "predicted: ", pred_lat)
    input_shape = list(model(get_inputs_by_shapes([[*input_shape]], 1)).shape)[1:]
    return res["latency"].avg, pred_lat

## ------------- model level
sample_str = "ks55355773757755735757_e66643464363346436436_d22343"
def get_model_result(model_cls, sample_str):
    model = model_cls(sample_str)
    real, pred = profile_and_predict(model, [3, 224, 224], mark="")
    print("profiled: ", real, "predicted: ", pred)
    return real, pred

## ------------- block level
def get_mobilenet_torch_blocks(sample_str):
    from nas_models.blocks.torch.mobilenetv3_block import block_dict, BasicBlock
    from nas_models.search_space.mobilenetv3_space import MobileNetV3Space
    from nas_models.common import parse_sample_str
    
    width_mult = 1.0
    num_classes = 1000
    hw = 224
    space = MobileNetV3Space(width_mult=width_mult, num_classes=num_classes, hw=hw)

    sample_config = parse_sample_str(sample_str)

    blocks = []
    first_conv = block_dict['first_conv'](hwin=hw, cin=3, cout=space.stage_width[0])
    first_mbconv = block_dict['first_mbconv'](
        hwin=hw//2,
        cin=space.stage_width[0],
        cout=space.stage_width[1]
    )
    blocks.append(first_conv)
    blocks.append(first_mbconv)

    hwin = hw // 2
    cin = space.stage_width[1]
    block_idx = 0
    for strides, cout, max_depth, depth, act, se in zip(
        space.stride_stages[1:], space.stage_width[2:], 
        space.num_block_stages[1:], sample_config['d'],
        space.act_stages[1:], space.se_stages[1:]
    ):
        for i in range(depth):
            k = sample_config['ks'][block_idx + i]
            e = sample_config['e'][block_idx + i]
            strides = 1 if i > 0 else strides
            # print(hwin, cin, cout, k, strides)
            blocks.append(block_dict['mbconv'](hwin, cin, cout, kernel_size=k, expand_ratio=e, strides=strides,
                act=act, se=int(se)))
            cin = cout 
            hwin //= strides
        block_idx += max_depth
    # blocks = nn.Sequential(*blocks)

    final_expand = block_dict['final_expand'].build_from_config(space.block_configs[-3])
    blocks.append(final_expand)
    feature_mix = block_dict['feature_mix'].build_from_config(space.block_configs[-2])
    blocks.append(feature_mix)
    logits = block_dict['logits'].build_from_config(space.block_configs[-1])
    blocks.append(logits)
    return blocks


def get_resnet_torch_blocks(sample_str):
    from nas_models.blocks.torch.resnet_block import InputStem, BConv, Logits
    from nas_models.search_space.resnet_space import ResNetSpace
    from nas_models.common import parse_sample_str
    
    num_classes = 1000
    hw = 224
    
    blocks = []
    
    space = ResNetSpace(num_classes, hw)
    sample_config = parse_sample_str(sample_str)

    input_stem_w0, input_stem_w1, *bconv_w_list  = sample_config['w']
    input_stem_d, *bconv_d_list = sample_config['d']

    # add input_stem
    input_stem_skipping = input_stem_d != max(space.depth_list)
    midc = space.mid_input_channel[input_stem_w0]
    cin = space.input_channel[input_stem_w1]
    input_stem = InputStem(hw, cin, midc, input_stem_skipping)
    blocks.append(input_stem)

    # add bottleneck blocks
    block_idx = 0
    hwin = hw // 4
    for w_idx, stage_width_list, d, base_depth, strides in zip(bconv_w_list, space.stage_width_list,
        bconv_d_list, ResNetSpace.BASE_DEPTH_LIST, space.stride_list):
        width = stage_width_list[w_idx]
        for i in range(base_depth + d):
            s = 1 if i > 0 else strides
            expand_ratio = sample_config['e'][block_idx + i]
            print(hwin, cin, width, expand_ratio, s)
            blocks.append(BConv(hwin, cin, width, expand_ratio=expand_ratio, strides=s))
            hwin //= s
            cin = width
        block_idx += base_depth + max(space.depth_list)
    # blocks = nn.Sequential(*blocks)

    # add classifier
    logits = Logits(hwin, cin, num_classes)
    blocks.append(logits)
    return blocks


def get_resnet_test_blocks(sample_str):
    from nas_models.blocks.torch.resnet_block import InputStem, BConv, Logits
    from nas_models.search_space.resnet_space import ResNetSpace
    from nas_models.common import parse_sample_str
    
    num_classes = 1000
    hw = 224
    
    blocks = []
    blocks_model = []
    
    space = ResNetSpace(num_classes, hw)
    sample_config = parse_sample_str(sample_str)

    input_stem_w0, input_stem_w1, *bconv_w_list  = sample_config['w']
    input_stem_d, *bconv_d_list = sample_config['d']

    # add input_stem
    input_stem_skipping = input_stem_d != max(space.depth_list)
    midc = space.mid_input_channel[input_stem_w0]
    cin = space.input_channel[input_stem_w1]
    input_stem = InputStem(hw, cin, midc, input_stem_skipping)
    blocks.append(input_stem)
    blocks_model.append(nn.Sequential(*blocks))

    # add bottleneck blocks
    block_idx = 0
    hwin = hw // 4
    for w_idx, stage_width_list, d, base_depth, strides in zip(bconv_w_list, space.stage_width_list,
        bconv_d_list, ResNetSpace.BASE_DEPTH_LIST, space.stride_list):
        width = stage_width_list[w_idx]
        for i in range(base_depth + d):
            s = 1 if i > 0 else strides
            expand_ratio = sample_config['e'][block_idx + i]
            # print(hwin, cin, width, expand_ratio, s)
            blocks.append(BConv(hwin, cin, width, expand_ratio=expand_ratio, strides=s))
            blocks_model.append(nn.Sequential(*blocks))
            hwin //= s
            cin = width
        block_idx += base_depth + max(space.depth_list)
    # blocks = nn.Sequential(*blocks)

    # add classifier
    logits = Logits(hwin, cin, num_classes)
    blocks.append(logits)
    blocks_model.append(nn.Sequential(*blocks))
    return blocks_model

def get_resnet_test_result(sample_str):
    real_collection, pred_collection = [], []
    input_shape = [3, 224, 224]
    for block in get_resnet_test_blocks(sample_str):
        real, pred = profile_and_predict(block, input_shape)
        real_collection.append(real)
        pred_collection.append(pred)
    print('block result in models: ', real_collection)

def get_block_result(sample_str, model='mobilenet'):
    real_collection, pred_collection = [], []
    input_shape = [3, 224, 224]
    if model == 'mobilenet':
        get_torch_blocks = get_mobilenet_torch_blocks
    elif model == 'resnet':
        get_torch_blocks = get_resnet_torch_blocks
    for i, block in enumerate(get_torch_blocks(sample_str)):
        real, pred = profile_and_predict(block, input_shape, mark=str(i))      
        input_shape = list(block(get_inputs_by_shapes([[*input_shape]], 1)).shape)[1:]
        real_collection.append(real)
        pred_collection.append(pred)
        # time.sleep(10)
        # break
    # block = get_torch_blocks(sample_str)[:2]
    # block = nn.Sequential(*block)
    # real, pred = profile_and_predict(block, input_shape)      
    # input_shape = list(block(get_inputs_by_shapes([[*input_shape]], 1)).shape)[1:]
    # real_collection.append(real)
    # pred_collection.append(pred)
    block_sum = []
    sum_ = 0
    for i in real_collection:
        sum_ += i
        block_sum.append(sum_)
    print('sum of block result: ', block_sum)
    return sum(real_collection), sum(pred_collection)


def model_level_test_mobilenetv3():
    model_cls = MobileNetV3Net
    sample_strs = [
        "ks33575373355333733735_e36436643443366644444_d34224",
        "ks35557755553357557577_e34444634446634344346_d32422",
        "ks35573553777537353577_e66663643664464634434_d34434",
        "ks35575755357375333535_e34666346446634666633_d33243",
        "ks35733755577537357533_e43646344343444466666_d44433",
        "ks37757555775335335773_e64343634434466646363_d43234",
        "ks37773735737755577555_e44643443334363446366_d32423",
        "ks53553373573735557757_e34636464334363443346_d42324",
        "ks53555333377537333333_e33633333633343636334_d32432",
        "ks55553557735733337735_e36464663366646633664_d22224",
        "ks55555733737375537357_e34644636646344434364_d24323",
        "ks55573357337355575377_e33343463434364663346_d22422",
        "ks55737375555373577575_e36446363464646643466_d23242",
        "ks55753555755337375337_e66344643364433434463_d34343",
        "ks57335333733333533377_e33346433336463334364_d43234",
        "ks57337553533753375775_e44663363644434663633_d33233",
        "ks57375737773337373753_e33364336363434633364_d22332",
        "ks57557335355733777337_e63444464363664336346_d23344",
        "ks57733337777753577735_e63646443644334363433_d24234",
        "ks73733375355333755335_e33344646466636636466_d23434",
        "ks75337773755575777735_e36364334333636463364_d32433",
        "ks75355577775533333577_e33463434364443336334_d32433",
        "ks75373355357337757553_e43436636646446446663_d34342",
        "ks75577553557535557753_e46434336343336466343_d32342",
        "ks77333575553355355757_e44663344344363346444_d24423",
        "ks77533353535353555375_e43344646446663636433_d43243",
        "ks77533573753375577735_e64334433446646446333_d43422",
        "ks77575333733335375335_e44364434346443664444_d44322",
        "ks77773333355577337577_e33336464633644643333_d43434",
        "ks77773533335735575575_e66466646643433364334_d24243"
    ]
    model_res, blocks_res = [], []
    pred_res = []
    for sample_str in sample_strs:
        real, pred = get_model_result(model_cls, sample_str)
        real_collection, pred_collection = get_block_result(sample_str)
        # print(pred_collection, pred)
        assert int(pred) == int(pred_collection)
        model_res.append(real)
        blocks_res.append(real_collection)
        pred_res.append(pred)
        # break

    print(model_res)
    print(blocks_res)
    print(pred_res)

    from nn_meter.dataset.bench_dataset import latency_metrics
    # first result
    # [12.249987502582371, 10.697516263462603, 15.283254371024668, 12.977135782130063, 15.040200399234891, 16.805960782803595, 12.333641056902707, 12.11845989804715, 10.843409495428205, 9.86639161594212, 10.824401904828846, 8.921326529234648, 12.164815440773964, 16.722270911559463, 13.340506758540869, 11.978719434700906, 10.842160694301128, 13.073826101608574, 13.135424223728478, 13.751582726836205, 12.951741521246731, 11.028801458887756, 14.39663636032492, 12.55843972787261, 12.064272919669747, 13.979026176966727, 14.876921479590237, 13.215321684256196, 19.26913076546043, 12.96588427387178]
    # [13.174293381161988, 12.25960579700768, 17.13364808820188, 14.944951985962689, 17.91127840988338, 17.945631546899676, 13.884303728118539, 13.503874726593494, 12.722029406577349, 11.452783001586795, 12.793498081155121, 10.536506134085357, 14.052079082466662, 16.62240343634039, 14.206458372063935, 14.229186330921948, 12.51209313981235, 14.730160813778639, 13.773103589192033, 14.5341558707878, 14.315508562140167, 12.821959354914725, 16.43644588533789, 14.975599735043943, 13.641426763497293, 16.015005614608526, 16.31128814537078, 15.228850464336574, 18.268028628081083, 15.642286906950176]
    # (1.7215729352537374, 11.978833812405412, 0.11829990383432135, 0.06666666666666667, 0.3, 0.8)
    
    # result after remove QuantizeLinear & DequantizeLinear
    # [9.342620000000002, 8.182479999999998, 11.404040000000002, 10.08162, 13.254920000000002, 13.2684, 10.170020000000001, 9.783140000000001, 8.635800000000001, 7.702179999999999, 8.8521, 7.41998, 8.944780000000002, 12.61208, 10.764639999999998, 10.310860000000002, 10.577279999999998, 10.764440000000002, 10.765979999999997, 9.082559999999999, 10.420379999999998, 9.37512, 11.47106, 10.14862, 9.679960000000001, 12.22434, 12.278339999999998, 11.985640000000002, 15.258859999999999, 10.443160000000002]
    # [10.221179999999999, 9.758600000000001, 13.163999999999998, 11.53982, 14.522059999999998, 14.72538, 11.463220000000002, 11.39338, 10.053359999999998, 8.900220000000001, 10.440559999999998, 8.685420000000002, 10.679039999999999, 14.544319999999997, 12.348980000000003, 11.2913, 10.923139999999997, 12.244879999999998, 12.153740000000003, 10.616300000000003, 11.88338, 11.039360000000002, 13.03272, 11.530840000000001, 10.90986, 13.671019999999999, 14.16352, 13.373560000000003, 17.180359999999997, 11.952599999999999]
    # (1.4737785504930285, 12.4473670690377, 0.12336105341837036, 0.03333333333333333, 0.16666666666666666, 0.8666666666666667)
    
    # result for sum all ops to get latency
    # [8.026580000000003, 7.772280000000001, 10.5196, 9.344479999999999, 11.913460000000002, 12.501159999999997, 9.56342, 9.053020000000002, 8.133099999999999, 7.854, 7.952559999999999, 6.736300000000001, 8.47126, 12.158800000000003, 10.07452, 9.20924, 9.086239999999998, 10.05712, 10.05788, 8.446039999999998, 9.62714, 8.65398, 10.74468, 9.501840000000001, 8.87132, 11.50596, 11.84172, 11.147699999999999, 14.49302, 9.82726]
    # [8.49632, 8.342180000000003, 11.16736, 9.905640000000002, 12.655740000000002, 13.015919999999996, 9.944059999999999, 9.475919999999999, 8.497860000000001, 7.481859999999998, 8.44068, 7.1713, 9.153599999999999, 12.431899999999999, 10.566880000000001, 9.54688, 9.511119999999996, 10.57454, 10.569579999999998, 8.87504, 10.31174, 9.091620000000002, 11.35386, 9.91204, 9.368139999999999, 12.023379999999998, 12.3719, 11.655459999999998, 14.9095, 10.317619999999996]
    # [26.341806907935613, 27.20505215899734, 35.20916452192449, 28.637422048872967, 37.702116108204876, 34.07564297006186, 31.213680048573693, 29.85215199247164, 27.007448819607276, 24.46454196818721, 28.420916471120343, 22.030261555173688, 31.188521062243865, 33.747848174142696, 29.35195628006773, 27.421330755695557, 25.933478885403847, 29.791144514489353, 28.379974639648673, 27.703329620446024, 30.344291164359607, 26.794505686838953, 33.22437010562538, 30.52949935097662, 25.819817646594494, 32.3203937505037, 34.3826375937084, 34.682556804272586, 37.27763254641041, 36.41181476397171]
    # (0.5025371115450077, 5.012782061421272, 0.04908553433985347, 0.6333333333333333, 1.0, 1.0)
    print(latency_metrics(model_res, blocks_res))

def model_level_test_resnet():
    model_cls = ResNetNet
    sample_strs = [
        "d00101_e352525352520252025202025253535353520_w122210",
        "d00112_e202525202025252520352525202025252525_w012111",
        "d00210_e252525252520253520352520352520202025_w102021",
        "d01002_e252035253525253535253525252025352025_w000012",
        "d01002_e352020352035353520252025352035202020_w200111",
        "d01010_e253520253520202525203525252520202520_w200100",
        "d01122_e252025352025352520252535352020203520_w200012",
        "d01222_e352025253520202535352535352025353535_w002110",
        "d02101_e203520203520202035252025352025352535_w201112",
        "d02101_e352525202520253520352535352535252020_w021011",
        "d02112_e202035252525252525202525203535202025_w201110",
        "d02121_e352525353535252020203535202535202035_w122011",
        "d02200_e352525202025202520202525252525203525_w122002",
        "d02201_e252535252525352520352035203520352535_w001111",
        "d02202_e202520203520252525352025202535352025_w122211",
        "d02221_e352520353535252535202025202520252020_w012020",
        "d20012_e202525202525253535203525252535252020_w100110",
        "d20102_e353535203535203535353535252025253525_w122000",
        "d20111_e352035202020352535252025203525352025_w200022",
        "d20201_e353520203520202535253535203535203520_w111010",
        "d20211_e252020202020352020352035353535252020_w121111",
        "d20220_e202525253535202025202525203535202020_w222110",
        "d21201_e353535252525202020202525352535253520_w021001",
        "d22102_e352535353535352525203525352520353525_w221221",
        "d22112_e353535202025252025203535353525203525_w001000",
        "d22120_e353535352535353535253535352035353535_w112111",
        "d22201_e202035253535252525353535353525252525_w200010",
        "d22210_e203535352520353525353520252520352535_w000201",
        "d22211_e202025352035202520252520202520352535_w110021",
        "d22212_e203535202520202520353535352520252020_w011000"
    ]
    model_res, blocks_res = [], []
    pred_res = []
    for sample_str in sample_strs:
        real, pred = get_model_result(model_cls, sample_str)
        real_collection, pred_collection = get_block_result(sample_str, 'resnet')
        print(pred_collection, pred)
        assert int(pred) == int(pred_collection)
        model_res.append(real)
        blocks_res.append(real_collection)
        pred_res.append(pred)
        # break

    print(model_res)
    print(blocks_res)
    print(pred_res)

    from nn_meter.dataset.bench_dataset import latency_metrics
    
    # results for sum all ops to get latency
    # [36.79786, 34.92096000000001, 36.750060000000005, 37.9912, 28.970559999999992, 24.76906, 36.28773999999999, 45.67828, 38.39065999999999, 35.02876, 34.761559999999996, 43.94900000000001, 34.89722, 38.22394, 46.35938000000001, 44.13834, 30.094380000000005, 36.91518000000001, 37.614380000000004, 34.57058, 36.983, 37.169360000000005, 34.71366, 52.68150000000001, 34.655080000000005, 51.3388, 33.650760000000005, 40.02768, 35.21833999999999, 33.64902]
    # [47.22566, 42.961040000000004, 44.83162, 41.56101999999999, 36.41630000000001, 32.01864, 42.909819999999996, 52.32263999999999, 46.25784000000001, 45.23662000000001, 42.47166000000001, 54.704840000000004, 45.005860000000006, 46.559819999999995, 56.80506, 54.50118000000001, 36.80226, 46.25881999999999, 44.101259999999996, 42.535239999999995, 46.4788, 47.710139999999996, 44.175139999999985, 62.78992000000001, 42.87104000000001, 60.85902, 41.25397999999999, 46.17598, 43.6857, 42.541399999999996]
    # [76.04348404109233, 69.06172428111192, 73.47869580944646, 67.77748367871435, 61.685110641128304, 56.78784718203669, 71.1620100188935, 87.35374405566168, 78.96811289458691, 76.11531286424075, 75.20282360557562, 92.33239298201126, 79.15308239538908, 80.44076730607894, 95.69401161038058, 93.22630201612326, 61.37720674125099, 75.99636795367911, 73.5766097448098, 70.10928061829738, 75.10495416786473, 81.5636556854022, 75.35491315730046, 105.81017567657912, 75.95877868522192, 102.38655510928733, 75.11052472159206, 81.21617612471877, 75.3439708064682, 75.23731322911983]
    # (8.58708591153716, 18.68874748807805, 0.18667194985253258, 0.0, 0.03333333333333333, 0.13333333333333333)
    
    # results after remove QuantizeLinear & DequantizeLinear
    # [36.97712, 35.196200000000005, 36.97808, 36.745180000000005, 29.957579999999997, 25.209760000000003, 37.48668, 44.47071999999999, 39.63786, 38.199819999999995, 35.31798, 45.14581999999999, 34.56932, 39.13926, 46.7138, 45.781459999999996, 30.99966, 39.91578, 39.16574, 35.15452, 37.929120000000005, 38.296479999999995, 36.49981999999999, 57.950900000000004, 35.90612, 52.61282, 34.55532, 38.887359999999994, 36.608, 34.7696]
    # [46.29138, 45.141059999999996, 46.56968, 43.97302, 37.73338, 33.82616, 44.781299999999995, 52.192919999999994, 48.24508000000001, 47.420579999999994, 44.73595999999999, 56.326620000000005, 46.77420000000001, 48.45206000000001, 59.107380000000006, 57.39456000000001, 38.46841999999999, 48.072919999999996, 46.37528000000001, 44.741159999999994, 48.222280000000005, 50.33372, 46.47892, 64.5951, 44.21068, 63.123439999999995, 43.22595999999999, 48.200379999999996, 45.571039999999996, 44.796600000000005]
    # [76.04348404109233, 69.06172428111192, 73.47869580944646, 67.77748367871435, 61.685110641128304, 56.78784718203669, 71.1620100188935, 87.35374405566168, 78.96811289458691, 76.11531286424075, 75.20282360557562, 92.33239298201126, 79.15308239538908, 80.44076730607894, 95.69401161038058, 93.22630201612326, 61.37720674125099, 75.99636795367911, 73.5766097448098, 70.10928061829738, 75.10495416786473, 81.5636556854022, 75.35491315730046, 105.81017567657912, 75.95877868522192, 102.38655510928733, 75.11052472159206, 81.21617612471877, 75.3439708064682, 75.23731322911983]
    # (9.411722336011975, 19.856903848646077, 0.19670848567057997, 0.0, 0.0, 0.06666666666666667)
    
    # results after remove QuantizeLinear & DequantizeLinear & Transpose
    # [38.244499999999995, 35.80914, 36.69144, 37.04628, 32.13871999999999, 24.690119999999997, 37.867360000000005, 44.223380000000006, 41.63968, 35.93443999999999, 35.30984, 44.99830000000001, 34.78956, 39.040940000000006, 46.30707999999999, 46.356539999999995, 29.81662, 37.80552, 39.08146, 35.55539999999999, 37.561119999999995, 38.11525999999999, 36.07883999999999, 54.12176, 35.48879999999999, 51.61104000000001, 34.148900000000005, 38.87028, 36.067620000000005, 35.365559999999995]
    # [46.99741999999999, 43.01212, 44.923939999999995, 41.88672, 36.25144, 32.290519999999994, 43.29751999999999, 51.745020000000004, 46.1495, 45.47015999999999, 42.649899999999995, 53.991400000000006, 44.2539, 46.69367999999999, 56.669639999999994, 53.80004, 36.99452, 47.189519999999995, 44.570360000000015, 42.44876, 46.114399999999996, 47.900919999999985, 44.00124, 62.52978, 42.49464000000001, 58.63316, 41.37680000000002, 45.96134000000001, 43.764959999999995, 42.74463999999999]
    # [76.04348404109233, 69.06172428111192, 73.47869580944646, 67.77748367871435, 61.685110641128304, 56.78784718203669, 71.1620100188935, 87.35374405566168, 78.96811289458691, 76.11531286424075, 75.20282360557562, 92.33239298201126, 79.15308239538908, 80.44076730607894, 95.69401161038058, 93.22630201612326, 61.37720674125099, 75.99636795367911, 73.5766097448098, 70.10928061829738, 75.10495416786473, 81.5636556854022, 75.35491315730046, 105.81017567657912, 75.95877868522192, 102.38655510928733, 75.11052472159206, 81.21617612471877, 75.3439708064682, 75.23731322911983]
    # (7.684643911669558, 16.860289772214387, 0.16744478826959042, 0.0, 0.03333333333333333, 0.3)
    print(latency_metrics(model_res, blocks_res))


if __name__ == '__main__':
    # model_level_test_mobilenetv3()
    model_level_test_resnet()
    # get_resnet_test_result()
    
    # sample_str = 'd00101_e352525352520252025202025253535353520_w122210'
    # get_resnet_test_result(sample_str)
    # get_block_result(sample_str, model='resnet')