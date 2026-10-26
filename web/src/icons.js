// 只注册用到的图标（全部注册会让主包大很多）。模板里写 icon="Refresh" 或 <Refresh /> 前，先在这里加上
import {
  ArrowDown, ArrowLeft, Box, Check, CircleClose, Clock, Close, Collection, CopyDocument, Cpu, Document, Download,
  Edit, EditPen, Files, List, Odometer, PieChart, Plus, PriceTag, QuestionFilled, Reading, Refresh, RefreshLeft,
  Search, Select, Stamp, TopRight, Upload, VideoPause, VideoPlay, Fold, Expand, FolderOpened, Monitor, Sunny, Moon,
  Link, MoreFilled, Warning, ArrowRight, ChatDotRound,
} from '@element-plus/icons-vue'

const ICONS = {
  ArrowDown, ArrowLeft, Box, Check, CircleClose, Clock, Close, Collection, CopyDocument, Cpu, Document, Download,
  Edit, EditPen, Files, List, Odometer, PieChart, Plus, PriceTag, QuestionFilled, Reading, Refresh, RefreshLeft,
  Search, Select, Stamp, TopRight, Upload, VideoPause, VideoPlay, Fold, Expand, FolderOpened, Monitor, Sunny, Moon,
  Link, MoreFilled, Warning, ArrowRight, ChatDotRound,
}

export default {
  install(app) {
    for (const [name, comp] of Object.entries(ICONS)) app.component(name, comp)
  },
}
