// Generado por xlinux. No editar.
//
// En Linux no existe ibtool, así que no se puede compilar Main.storyboard.
// Esta subclase del SceneDelegate del proyecto crea por código la ventana con
// el FlutterViewController, que es todo lo que hace Main.storyboard.
import Flutter
import UIKit

class FlutterLinuxSceneDelegate: SceneDelegate {
  override func scene(
    _ scene: UIScene,
    willConnectTo session: UISceneSession,
    options connectionOptions: UIScene.ConnectionOptions
  ) {
    if window == nil, let windowScene = scene as? UIWindowScene {
      let window = UIWindow(windowScene: windowScene)
      window.rootViewController = FlutterViewController(project: nil, nibName: nil, bundle: nil)
      self.window = window
      window.makeKeyAndVisible()
    }
    super.scene(scene, willConnectTo: session, options: connectionOptions)
  }
}
